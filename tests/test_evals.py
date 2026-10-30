"""Tests for the evaluation harness: loading, metrics, artifacts, runner."""

import hashlib
import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.events import AgentEvent
from app.agent.fix import RunBudgets, RunStatus
from app.db import Database
from app.evals import (
    BenchIssue,
    EvalError,
    EvalResult,
    checkout_commit,
    comparison_table,
    compute_metrics,
    evaluate_run,
    load_benchmark,
    load_results,
    metrics_by_model,
    prepare_repo,
    run_eval,
)
from app.sandbox import ExecStatus
from tests.fakes import FakeLLM, FakeSandbox, execution, make_diff, plan_args, reply, tool_call
from tests.test_report import PATCH
from tests.test_report import result as make_result

ROOT = Path(__file__).parent.parent
BENCH = ROOT / "evals" / "benchmark_issues.jsonl"
REL = "tests/test_cart.py::test_average_price_of_empty_cart_is_zero"


def line(**over):
    base = {
        "id": "a-1", "repo": "dir:evals/repos/avg_price", "commit": None,
        "issue": {"title": "t", "body": "b"}, "relevant_test": REL,
        "expected_outcome": "passed", "notes": "",
    }
    base.update(over)
    return json.dumps(base)


def write(tmp_path, *lines):
    path = tmp_path / "bench.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("app.tools.search.shutil.which", lambda name: None)


# --- loading the benchmark ----------------------------------------------------

def test_loads_valid_file_and_skips_blank_lines(tmp_path):
    path = write(tmp_path, line(id="a-1"), "", line(id="b-2", notes="hard"))
    issues = load_benchmark(path)
    assert [i.id for i in issues] == ["a-1", "b-2"]
    first = issues[0]
    assert isinstance(first, BenchIssue)
    assert first.title == "t" and first.body == "b" and first.relevant_test == REL
    assert first.expected_outcome == "passed" and first.commit is None
    assert issues[1].notes == "hard"


def test_shipped_seed_benchmark_is_valid():
    issues = load_benchmark(BENCH)
    assert len(issues) == 3
    for issue in issues:
        repo = ROOT / issue.repo.removeprefix("dir:")
        assert repo.is_dir() and list(repo.glob("tests/test_*.py"))
        assert issue.expected_outcome == "passed"


@pytest.mark.parametrize(
    "bad,needle",
    [
        ("{not json", "line 1"),
        ("[1, 2]", "object"),
        (json.dumps({"id": "x"}), "repo"),
        (line(expected_outcome="maybe"), "expected_outcome"),
        (line(expected_outcome=None), "expected_outcome"),
        (line(id="bad id!"), "id"),
        (line(id=""), "id"),
        (line(relevant_test=""), "relevant_test"),
        (line(issue={"body": "no title"}), "title"),
        (line(issue="text"), "issue"),
        (line(commit="abc1234"), "commit"),  # dir repos have no commit
        (line(repo="https://github.com/o/r", commit=None), "commit"),
        (line(repo="https://github.com/o/r", commit="zzzz"), "commit"),
        (line(repo="https://evil.example/o/r", commit="abc1234"), "repo"),
        (line(repo="ftp://x"), "repo"),
    ],
)
def test_invalid_entries_rejected_with_a_helpful_message(tmp_path, bad, needle):
    with pytest.raises(EvalError, match=needle):
        load_benchmark(write(tmp_path, bad))


def test_duplicate_ids_rejected(tmp_path):
    with pytest.raises(EvalError, match="duplicate"):
        load_benchmark(write(tmp_path, line(id="a"), line(id="a")))


def test_empty_and_missing_files_rejected(tmp_path):
    with pytest.raises(EvalError, match="no issues"):
        load_benchmark(write(tmp_path, ""))
    with pytest.raises(EvalError, match="not found"):
        load_benchmark(tmp_path / "nope.jsonl")


def test_github_entry_with_commit_is_accepted(tmp_path):
    issues = load_benchmark(
        write(tmp_path, line(repo="https://github.com/o/r", commit="a1b2c3d4"))
    )
    assert issues[0].commit == "a1b2c3d4"


# --- preparing repos ----------------------------------------------------------

def test_prepare_dir_repo_makes_a_git_repo_copy(tmp_path):
    issue = load_benchmark(BENCH)[0]
    path = prepare_repo(issue, ROOT)
    try:
        assert (path / ".git").is_dir() and (path / "shop" / "cart.py").exists()
        assert not any(p.name == "__pycache__" for p in path.rglob("*"))
        log = subprocess.run(["git", "-C", str(path), "log", "--oneline"], capture_output=True,
                             text=True).stdout
        assert log.strip()
        # the original seed repo is untouched and not a git repo
        assert not (ROOT / "evals" / "repos" / "avg_price" / ".git").exists()
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def test_prepare_dir_repo_rejects_paths_outside_the_base(tmp_path):
    issue = BenchIssue("x", "dir:../../etc", None, "t", "b", REL, "passed", "")
    with pytest.raises(EvalError, match="outside"):
        prepare_repo(issue, ROOT)
    missing = BenchIssue("x", "dir:evals/repos/nope", None, "t", "b", REL, "passed", "")
    with pytest.raises(EvalError, match="not found"):
        prepare_repo(missing, ROOT)


def test_checkout_commit_uses_fixed_git_commands(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    checkout_commit("https://github.com/o/r", "a1b2c3d4", tmp_path / "dest")
    flat = [" ".join(c) for c in seen]
    assert any("fetch --depth 1 origin a1b2c3d4" in c for c in flat)
    assert any("checkout" in c and "FETCH_HEAD" in c for c in flat)
    assert all(isinstance(c, list) for c in seen)  # no shell strings


def test_checkout_commit_rejects_bad_input(tmp_path):
    with pytest.raises(EvalError):
        checkout_commit("https://evil.example/o/r", "a1b2c3d4", tmp_path / "d")
    with pytest.raises(EvalError):
        checkout_commit("https://github.com/o/r", "--upload-pack=x", tmp_path / "d")


# --- evaluating one run -------------------------------------------------------

def ev(kind, **data):
    return AgentEvent(ts=1.0, state="PATCH", kind=kind, data=data)


def bench_issue(**kw):
    base = dict(id="a-1", repo="dir:x", commit=None, title="t", body="b", relevant_test=REL,
                expected_outcome="passed", notes="")
    base.update(kw)
    return BenchIssue(**base)


def test_evaluate_a_resolved_run():
    events = [
        ev("tool_result", name="read_file", ok=True, chars=10),
        ev("tool_result", name="search_code", ok=False, chars=10),
        ev("plan_rejected", reason="x"), ev("plan_accepted", files=1, steps=1),
        ev("patch_rejected", reason="x"), ev("patch_proposed", files=1, added=1, removed=0),
        ev("llm_call", input_tokens=1000, output_tokens=500, latency_s=1.0, usage_missing=False),
    ]
    run = make_result(
        RunStatus.PASSED, events=events,
        baseline=execution(ExecStatus.FAILED, [REL]),
        executions=[execution(ExecStatus.PASSED)], patches=[PATCH],
        input_tokens=1000, output_tokens=500, steps=4, repairs_used=0,
    )
    r = evaluate_run(run, bench_issue(), "m1", 12.5, Decimal("0.006"), False)
    assert isinstance(r, EvalResult)
    assert (r.model, r.issue_id, r.status) == ("m1", "a-1", "passed")
    assert r.resolved and r.baseline_relevant_failed and r.final_passed and r.patched
    assert (r.tool_ok, r.tool_total) == (3, 6)  # 1/2 tool results, 1/2 plans, 1/2 patches ok
    assert r.latency_s == 12.5 and r.cost == Decimal("0.006")
    assert (r.input_tokens, r.output_tokens, r.steps, r.repairs_used) == (1000, 500, 4, 0)
    assert r.expected_outcome == "passed" and r.error is None


def test_passed_but_relevant_test_was_not_failing_is_not_resolved():
    run = make_result(
        RunStatus.PASSED, baseline=execution(ExecStatus.FAILED, ["tests/test_other.py::test_x"]),
        executions=[execution(ExecStatus.PASSED)], patches=[PATCH], events=[],
    )
    r = evaluate_run(run, bench_issue(), "m", 1.0, None, False)
    assert r.final_passed and not r.resolved and not r.baseline_relevant_failed


def test_failed_and_unpatched_runs():
    failed = make_result(
        RunStatus.FAILED, baseline=execution(ExecStatus.FAILED, [REL]),
        executions=[execution(ExecStatus.FAILED, [REL])], patches=[PATCH], events=[],
    )
    r = evaluate_run(failed, bench_issue(), "m", 1.0, None, False)
    assert not r.resolved and not r.final_passed and r.patched
    aborted = make_result(RunStatus.ABORTED, "x", executions=[], patches=[], events=[],
                          baseline=None, plan=None, final_diff="")
    r = evaluate_run(aborted, bench_issue(), "m", 1.0, None, False)
    assert not r.patched and not r.final_passed and r.status == "aborted"


# --- metrics (exact numbers) --------------------------------------------------

def er(model="A", **kw):
    base = dict(
        model=model, issue_id="i", expected_outcome="passed", status="passed", reason=None,
        baseline_relevant_failed=True, final_passed=True, resolved=True, patched=True,
        repairs_used=0, steps=3, input_tokens=100, output_tokens=50, cost=Decimal("0.01"),
        cost_estimated=False, latency_s=10.0, tool_ok=8, tool_total=10, error=None, run_id=None,
    )
    base.update(kw)
    return EvalResult(**base)


@pytest.fixture
def model_a():
    return [
        er(),  # passed first try
        er(status="passed", repairs_used=1, tool_ok=9, cost=Decimal("0.03"), latency_s=30.0),
        er(status="failed", final_passed=False, resolved=False, repairs_used=2, tool_ok=5,
           cost=Decimal("0.02"), latency_s=20.0),
        er(status="aborted", patched=False, final_passed=False, resolved=False, tool_ok=0,
           tool_total=2, cost=None, latency_s=5.0),
    ]


def test_metrics_exact_numbers(model_a):
    m = compute_metrics(model_a)
    assert m.runs == 4
    assert m.patch_test_pass_rate == pytest.approx(2 / 3)  # 3 patched runs, 2 final-passing
    assert m.issue_resolution_rate == 0.5
    assert m.tool_call_success_rate == 22 / 32
    assert m.repair_success_rate == 0.5  # 2 runs needed repairs, 1 ended passed
    assert m.cost_per_issue == Decimal("0.02")  # mean of 0.01, 0.03, 0.02
    assert m.unknown_cost_runs == 1
    assert m.median_latency_s == 15.0  # median of 5, 10, 20, 30
    assert m.expectation_match_rate == 0.5


def test_metrics_when_nothing_to_measure():
    m = compute_metrics([er(status="aborted", patched=False, final_passed=False, resolved=False,
                            tool_ok=0, tool_total=0, cost=None)])
    assert m.patch_test_pass_rate is None
    assert m.repair_success_rate is None
    assert m.tool_call_success_rate is None
    assert m.cost_per_issue is None and m.unknown_cost_runs == 1
    empty = compute_metrics([])
    assert empty.runs == 0 and empty.issue_resolution_rate is None
    assert empty.median_latency_s is None


def test_metrics_are_grouped_by_model(model_a):
    grouped = metrics_by_model(model_a + [er("B", latency_s=7.0)])
    assert set(grouped) == {"A", "B"}
    assert grouped["A"].runs == 4 and grouped["B"].runs == 1
    assert grouped["B"].issue_resolution_rate == 1.0 and grouped["B"].median_latency_s == 7.0


def test_comparison_table_is_markdown(model_a):
    table = comparison_table(metrics_by_model(model_a + [er("B", cost=None)]))
    rows = table.strip().splitlines()
    assert rows[0].startswith("| Model |") and set(rows[1]) <= set("|- :")
    a = next(r for r in rows if r.startswith("| A "))
    assert "50.0%" in a and "66.7%" in a and "68.8%" in a  # resolution, patch pass, tool success
    assert "0.02" in a and "15.0 s" in a
    b = next(r for r in rows if r.startswith("| B "))
    assert "unknown" in b
    assert len(rows) == 4


def test_cost_per_issue_is_rounded_for_display():
    three = [er(cost=Decimal("0.00119084")), er(cost=Decimal("0.00204246")),
             er(cost=Decimal("0.00197204"))]
    m = compute_metrics(three)
    assert m.cost_per_issue == sum(r.cost for r in three) / 3  # exact value kept
    assert "| 0.00173511 |" in comparison_table({"A": m})


# --- running the benchmark ----------------------------------------------------

def good_llm(repo_dir, id_suffix=""):
    old = (repo_dir / "shop" / "cart.py").read_text()
    new = old.replace(
        "    return sum(prices) / len(prices)",
        "    if not prices:\n        return 0\n    return sum(prices) / len(prices)",
    )
    return FakeLLM(
        [
            reply(tool_call("read_file", {"path": "shop/cart.py"}, "r1")),
            reply(tool_call("submit_plan", plan_args(), "p1")),
            reply(tool_call("propose_patch", {"diff": make_diff("shop/cart.py", old, new)}, "pp")),
        ]
    )


AVG = ROOT / "evals" / "repos" / "avg_price"
FAILING = execution(ExecStatus.FAILED, [REL])
PASSING = execution(ExecStatus.PASSED)


async def run_it(tmp_path, models, llm_for, issues=None, **kw):
    issues = issues or load_benchmark(BENCH)[:1]
    out = tmp_path / "out"
    results = await run_eval(
        issues, models, llm_for, lambda: FakeSandbox(FAILING, PASSING), out,
        RunBudgets(), base_dir=ROOT, prices_for=lambda model: (Decimal("2"), Decimal("8")),
        benchmark_path=BENCH, **kw,
    )
    return results, out


async def test_run_eval_writes_artifacts_and_results(tmp_path):
    results, out = await run_it(tmp_path, ["m1"], lambda model: good_llm(AVG))
    (r,) = results
    assert r.status == "passed" and r.resolved and r.model == "m1"
    assert r.issue_id == "avg-price-empty-cart"
    assert r.cost == Decimal("0.0018")  # (300*2 + 150*8) / 1e6
    assert r.latency_s >= 0 and r.steps == 3
    folder = out / "m1" / "avg-price-empty-cart"
    assert "if not prices" in (folder / "patch.diff").read_text()
    trace = json.loads((folder / "trace.json").read_text())
    assert any(e["kind"] == "llm_call" for e in trace)
    tests = json.loads((folder / "tests.json").read_text())
    assert tests["baseline"]["status"] == "failed" and tests["after_patch"][0]["status"] == "passed"
    assert json.loads((folder / "result.json").read_text())["resolved"] is True
    summary = json.loads((out / "summary.json").read_text())
    assert summary["m1"]["issue_resolution_rate"] == 1.0
    assert "| m1 |" in (out / "comparison.md").read_text()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["models"] == ["m1"]
    assert meta["benchmark_sha256"] == hashlib.sha256(BENCH.read_bytes()).hexdigest()


async def test_run_eval_two_models_each_get_their_own_client(tmp_path):
    asked = []

    def llm_for(model):
        asked.append(model)
        return good_llm(AVG)

    results, out = await run_it(tmp_path, ["m1", "m2"], llm_for)
    assert asked == ["m1", "m2"]
    assert [r.model for r in results] == ["m1", "m2"]
    assert (out / "m2" / "avg-price-empty-cart" / "result.json").exists()
    assert set(json.loads((out / "summary.json").read_text())) == {"m1", "m2"}


async def test_run_eval_stores_runs_in_the_database(tmp_path):
    db = Database(tmp_path / "e.db")
    results, _ = await run_it(tmp_path, ["m1"], lambda m: good_llm(AVG), db=db)
    run = db.get_run(results[0].run_id)
    assert run.model == "m1" and run.status == "passed" and run.cost == Decimal("0.0018")
    db.close()


async def test_a_crash_in_one_run_does_not_stop_the_others(tmp_path):
    def llm_for(model):
        return FakeLLM([RuntimeError("boom")]) if model == "bad" else good_llm(AVG)

    results, out = await run_it(tmp_path, ["bad", "good"], llm_for)
    by_model = {r.model: r for r in results}
    assert by_model["bad"].status == "error" and "RuntimeError" in by_model["bad"].error
    assert by_model["good"].status == "passed"
    assert not by_model["bad"].resolved


async def test_spend_cap_stops_remaining_runs(tmp_path):
    db = Database(tmp_path / "e.db")
    results, out = await run_it(
        tmp_path, ["m1", "m2", "m3"], lambda m: good_llm(AVG), db=db,
        spend_cap=Decimal("0.001"),
    )
    assert [r.model for r in results] == ["m1"]  # m1 cost 0.0018, which reaches the cap
    assert "spend cap" in (out / "meta.json").read_text()
    db.close()


async def test_working_files_are_cleaned_up(tmp_path):
    await run_it(tmp_path, ["m1"], lambda m: good_llm(AVG))
    work = tmp_path / "cache" / "repopilot" / "work"
    assert not work.exists() or list(work.iterdir()) == []


async def test_results_round_trip_through_disk(tmp_path):
    results, out = await run_it(tmp_path, ["m1"], lambda m: good_llm(AVG))
    loaded = load_results(out)
    assert loaded == results
    assert loaded[0].cost == Decimal("0.0018")
