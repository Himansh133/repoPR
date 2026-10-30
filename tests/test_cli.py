"""CLI tests: fake LLM, fake sandbox and fake GitHub; real git clone and real SQLite."""

import io
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.cli import Deps, main, parse_github_slug
from app.config import Settings
from app.db import Database
from app.github import GitHubRateLimit, IssueNotFound
from app.llm import LLMError
from app.sandbox import ExecStatus, SandboxError
from tests.fakes import FakeLLM, FakeSandbox, execution, reply
from tests.test_fix_run import (
    CART,
    FAIL_NAME,
    FAILING,
    ISSUE,
    PASSING,
    RIGHT,
    investigate_script,
    propose,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
API_KEY = "sk-very-secret-key"
GH_TOKEN = "ghp_very_secret_token"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GMI_API_KEY", API_KEY)
    monkeypatch.setenv("GMI_MODEL", "test-model")
    monkeypatch.setenv("GITHUB_TOKEN", GH_TOKEN)
    monkeypatch.setenv("PRICE_IN_PER_M", "2")
    monkeypatch.setenv("PRICE_OUT_PER_M", "8")
    monkeypatch.setenv("MODEL_PRICES", '{"m1": ["2", "8"], "m2": ["1", "4"]}')
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setattr("app.tools.search.shutil.which", lambda name: None)
    monkeypatch.chdir(tmp_path)  # so no stray .env is read


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "remote", "add", "origin", "https://github.com/octo/demo.git")
    return root


@pytest.fixture
def issue_file(tmp_path):
    path = tmp_path / "issue.json"
    path.write_text(json.dumps({"title": ISSUE.title, "body": ISSUE.body, "labels": ["bug"]}))
    return path


class FakeGitHub:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def get_issue(self, owner, repo, number):
        self.calls.append((owner, repo, number))
        if self.error:
            raise self.error
        return ISSUE


def make_deps(llm=None, sandbox=None, github=None, clone=None):
    deps = Deps(settings=lambda: Settings(_env_file=None))
    deps.llm = lambda s: llm or FakeLLM([])
    deps.sandbox = lambda s: sandbox or FakeSandbox()
    deps.github = lambda s: github or FakeGitHub()
    if clone:
        deps.clone = clone
    return deps


def run_cli(argv, deps):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, deps=deps, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def good_run_deps(**kw):
    return make_deps(
        llm=FakeLLM([*investigate_script(), propose(CART, RIGHT)]),
        sandbox=FakeSandbox(FAILING, PASSING),
        **kw,
    )


def args(source, issue_file=None, *extra):
    base = ["run", "--repo", str(source), "--issue", "7"]
    if issue_file:
        base += ["--issue-file", str(issue_file)]
    return base + list(extra)


def db_of(tmp_path):
    return Database(tmp_path / "cli.db")


# --- slug parsing -------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("octo/demo", ("octo", "demo")),
        ("https://github.com/octo/demo", ("octo", "demo")),
        ("https://github.com/octo/demo.git", ("octo", "demo")),
        ("git@github.com:octo/demo.git", ("octo", "demo")),
        ("https://gitlab.com/octo/demo", None),
        ("nonsense", None),
        ("../x/y", None),
        ("a/b/c", None),
    ],
)
def test_parse_github_slug(text, expected):
    assert parse_github_slug(text) == expected


# --- run: outcomes and exit codes ---------------------------------------------

def test_successful_run_prints_report_and_saves_everything(source, issue_file, tmp_path):
    code, out, err = run_cli(args(source, issue_file), good_run_deps())
    assert code == 0, err
    assert "Status: PASSED" in out
    assert "## Proposed changes" in out and "## Test results" in out
    assert "Run #1" in out and "Branch: repopilot/issue-7" in out
    assert "0.0018" in out  # (300*2 + 150*8) / 1e6
    db = db_of(tmp_path)
    run = db.get_run(1)
    assert run.status == "passed" and run.model == "test-model"
    assert run.repo == "octo/demo" and run.issue_number == 7
    assert str(run.cost) == "0.0018"
    assert run.report and "Status: PASSED" in run.report
    assert Path(run.workdir).is_dir()
    shutil.rmtree(run.workdir, ignore_errors=True)


@pytest.mark.parametrize(
    "sandbox_script,code,status",
    [
        ((FAILING, FAILING, FAILING, FAILING), 1, "FAILED"),
        ((PASSING, PASSING), 3, "INCONCLUSIVE"),
        ((FAILING, execution(ExecStatus.VIOLATION, reason="timeout: too slow")), 4, "ABORTED"),
    ],
)
def test_exit_codes_follow_the_final_status(source, issue_file, sandbox_script, code, status):
    v0 = CART.replace("sum(prices)", "sum(prices) + 0")
    v1, v2 = v0.replace("+ 0", "+ 1"), v0.replace("+ 0", "+ 2")
    llm = FakeLLM(
        [*investigate_script(), propose(CART, v0), propose(v0, v1, "pp2"), propose(v1, v2, "pp3")]
    )
    deps = make_deps(llm=llm, sandbox=FakeSandbox(*sandbox_script))
    got, out, _ = run_cli(args(source, issue_file, "--cleanup"), deps)
    assert got == code
    assert f"Status: {status}" in out


def test_cleanup_removes_the_working_copy(source, issue_file, tmp_path):
    code, out, _ = run_cli(args(source, issue_file, "--cleanup"), good_run_deps())
    assert code == 0
    assert not Path(db_of(tmp_path).get_run(1).workdir).exists()


def test_crash_is_recorded_and_reported(source, issue_file, tmp_path):
    llm = FakeLLM([RuntimeError("kaboom")])
    deps = make_deps(llm=llm, sandbox=FakeSandbox(FAILING))
    code, out, err = run_cli(args(source, issue_file), deps)
    assert code == 4
    assert "crashed" in err and "RuntimeError" in err
    assert "Traceback" not in err
    run = db_of(tmp_path).get_run(1)
    assert run.status == "error"


def test_sandbox_error_is_an_aborted_run(source, issue_file, tmp_path):
    deps = make_deps(sandbox=FakeSandbox(SandboxError("Docker is not installed")))
    code, out, _ = run_cli(args(source, issue_file), deps)
    assert code == 4 and "Status: ABORTED" in out and "Docker is not installed" in out
    assert db_of(tmp_path).get_run(1).status == "aborted"


# --- run: getting the issue ---------------------------------------------------

def test_issue_is_fetched_from_github_using_the_origin_remote(source):
    gh = FakeGitHub()
    code, out, err = run_cli(args(source), good_run_deps(github=gh))
    assert code == 0, err
    assert gh.calls == [("octo", "demo", 7)]


def test_github_flag_overrides_the_slug(source):
    gh = FakeGitHub()
    run_cli(args(source, None, "--github", "me/other"), good_run_deps(github=gh))
    assert gh.calls == [("me", "other", 7)]


def test_missing_issue_on_github(source):
    gh = FakeGitHub(IssueNotFound("Issue #7 not found in octo/demo"))
    code, out, err = run_cli(args(source), make_deps(github=gh))
    assert code == 5 and "not found" in err and "Traceback" not in err


def test_github_rate_limit_message(source):
    gh = FakeGitHub(GitHubRateLimit("GitHub rate limit reached", 1893456000))
    code, _, err = run_cli(args(source), make_deps(github=gh))
    assert code == 5 and "rate limit" in err


def test_no_slug_and_no_issue_file_is_an_error(tmp_path):
    root = tmp_path / "plain"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    code, _, err = run_cli(args(root), make_deps())
    assert code == 5 and "--github" in err


def test_bad_issue_file(source, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    code, _, err = run_cli(args(source, bad), make_deps())
    assert code == 5 and "issue file" in err.lower()
    bad.write_text(json.dumps({"body": "no title"}))
    code, _, err = run_cli(args(source, bad), make_deps())
    assert code == 5 and "title" in err


# --- run: the repo argument ---------------------------------------------------

def test_repo_that_does_not_exist(tmp_path, issue_file):
    code, _, err = run_cli(args(tmp_path / "nope", issue_file), make_deps())
    assert code == 5 and "not found" in err.lower()


def test_repo_that_is_not_a_git_repo(tmp_path, issue_file):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, _, err = run_cli(args(plain, issue_file), make_deps())
    assert code == 5 and "git" in err.lower()


def test_github_url_is_cloned_then_removed(source, issue_file, tmp_path):
    cloned = []

    def fake_clone(url, dest):
        shutil.copytree(source, dest)
        cloned.append((url, Path(dest)))
        return Path(dest)

    deps = good_run_deps(clone=fake_clone)
    code, out, err = run_cli(
        ["run", "--repo", "https://github.com/octo/demo", "--issue", "7",
         "--issue-file", str(issue_file)],
        deps,
    )
    assert code == 0, err
    assert cloned[0][0] == "https://github.com/octo/demo"
    assert not cloned[0][1].exists()  # temporary clone is cleaned up
    assert db_of(tmp_path).get_run(1).repo == "octo/demo"
    shutil.rmtree(db_of(tmp_path).get_run(1).workdir, ignore_errors=True)


def test_non_github_url_rejected(issue_file):
    code, _, err = run_cli(
        ["run", "--repo", "https://evil.example/o/r", "--issue", "1", "--issue-file",
         str(issue_file)],
        make_deps(),
    )
    assert code == 5 and "github" in err.lower()


# --- configuration and budgets ------------------------------------------------

def test_missing_configuration_is_a_clear_message(source, issue_file, monkeypatch):
    monkeypatch.delenv("GMI_API_KEY")
    monkeypatch.delenv("GMI_MODEL")
    code, out, err = run_cli(args(source, issue_file), make_deps())
    assert code == 5
    assert "GMI_API_KEY" in err and "GMI_MODEL" in err and ".env" in err
    assert "Traceback" not in err and "ValidationError" not in err


def test_daily_run_limit_blocks_before_any_model_call(source, issue_file, tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_RUNS_PER_DAY", "1")
    deps = good_run_deps()
    assert run_cli(args(source, issue_file, "--cleanup"), deps)[0] == 0
    llm = FakeLLM([])
    code, out, err = run_cli(args(source, issue_file), make_deps(llm=llm))
    assert code == 5 and "run limit" in err
    assert llm.calls == []
    assert len(db_of(tmp_path).list_runs()) == 1  # the blocked attempt is not recorded


def test_daily_spend_cap_blocks(source, issue_file, monkeypatch):
    monkeypatch.setenv("DAILY_SPEND_CAP", "0.001")
    assert run_cli(args(source, issue_file, "--cleanup"), good_run_deps())[0] == 0  # costs 0.0018
    code, _, err = run_cli(args(source, issue_file), make_deps())
    assert code == 5 and "spend cap" in err


def test_unpriced_runs_are_blocked_unless_allowed(source, issue_file, monkeypatch, tmp_path):
    monkeypatch.delenv("PRICE_IN_PER_M")
    monkeypatch.delenv("PRICE_OUT_PER_M")
    code, _, err = run_cli(args(source, issue_file), make_deps())
    assert code == 5 and "PRICE_IN_PER_M" in err
    code, out, _ = run_cli(args(source, issue_file, "--allow-unpriced", "--cleanup"),
                           good_run_deps())
    assert code == 0 and "unknown" in out.lower()
    assert db_of(tmp_path).get_run(1).cost is None


def test_secrets_never_appear_in_output(source, issue_file):
    texts = []
    texts += run_cli(args(source, issue_file, "--cleanup"), good_run_deps())[1:]
    texts += run_cli(args(source, None), make_deps(github=FakeGitHub(IssueNotFound("x"))))[1:]
    texts += run_cli(["runs"], make_deps())[1:]
    assert API_KEY not in "".join(texts) and GH_TOKEN not in "".join(texts)


def test_llm_error_in_the_run_is_aborted_not_a_crash(source, issue_file):
    from app.llm import LLMRateLimitError

    llm = FakeLLM([LLMRateLimitError("Rate limited (429)")])
    code, out, _ = run_cli(args(source, issue_file), make_deps(llm=llm,
                           sandbox=FakeSandbox(FAILING)))
    assert code == 4 and "Status: ABORTED" in out and issubclass(LLMError, Exception)


# --- other commands -----------------------------------------------------------

def test_runs_show_and_usage(source, issue_file, tmp_path):
    run_cli(args(source, issue_file, "--cleanup"), good_run_deps())
    code, out, _ = run_cli(["runs"], make_deps())
    assert code == 0
    assert "octo/demo#7" in out and "passed" in out and "test-model" in out
    code, out, _ = run_cli(["show", "1"], make_deps())
    assert code == 0 and "Status: PASSED" in out
    code, out, _ = run_cli(["usage"], make_deps())
    assert code == 0
    assert "Runs: 1" in out and "300 in, 150 out" in out and "0.0018" in out
    today = datetime.now(UTC).date().isoformat()
    code, out, _ = run_cli(["usage", "--today"], make_deps())
    assert code == 0 and "Runs:" in out and today


def test_show_unknown_run(tmp_path):
    code, _, err = run_cli(["show", "99"], make_deps())
    assert code == 5 and "No run" in err


def test_runs_on_an_empty_database():
    code, out, _ = run_cli(["runs"], make_deps())
    assert code == 0 and "No runs" in out


def test_long_runs_labels_keep_their_end_and_leave_a_gap():
    from app.cli import _clip_left

    long = "/tmp/some/very/long/scratch/folder/sample_repo#1"
    clipped = _clip_left(long, 24)
    assert len(clipped) == 24 and clipped.startswith("…") and clipped.endswith("sample_repo#1")
    assert _clip_left("octo/demo#7", 24) == "octo/demo#7"


def test_bad_arguments_exit_with_argparse_code(source):
    with pytest.raises(SystemExit) as exc:
        run_cli(["run", "--repo", str(source)], make_deps())  # --issue missing
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        run_cli(["run", "--repo", str(source), "--issue", "abc"], make_deps())
    with pytest.raises(SystemExit):
        run_cli([], make_deps())


def test_fail_name_constant_is_used_in_reports(source, issue_file):
    code, out, _ = run_cli(args(source, issue_file, "--cleanup"), good_run_deps())
    assert FAIL_NAME in out and reply  # baseline failure appears in the test results


# --- eval and compare ---------------------------------------------------------

def eval_deps(scripts, sandbox_script=None):
    from tests.test_evals import AVG, good_llm

    deps = Deps(settings=lambda: Settings(_env_file=None))
    deps.llm = lambda s: (scripts.get(s.gmi_model) or good_llm(AVG))
    deps.sandbox = lambda s: FakeSandbox(*(sandbox_script or (FAILING, PASSING)))
    deps.github = lambda s: FakeGitHub()
    return deps


def eval_args(tmp_path, *extra, models="m1,m2"):
    from tests.test_evals import BENCH, ROOT

    return ["eval", "--models", models, "--issues", str(BENCH),
            "--out", str(tmp_path / "evalout"), "--base-dir", str(ROOT), *extra]


def test_eval_runs_every_model_and_prints_the_comparison(tmp_path):
    from tests.test_evals import BENCH, load_benchmark

    first_only = tmp_path / "one.jsonl"
    first_only.write_text(BENCH.read_text().splitlines()[0] + "\n")
    argv = eval_args(tmp_path)
    argv[argv.index(str(BENCH))] = str(first_only)
    code, out, err = run_cli(argv, eval_deps({}))
    assert code == 0, err
    assert "Planned 2 runs" in out
    assert "| m1 |" in out and "| m2 |" in out and "Resolved" in out
    assert (tmp_path / "evalout" / "comparison.md").exists()
    assert (tmp_path / "evalout" / "m2" / "avg-price-empty-cart" / "patch.diff").exists()
    db = db_of(tmp_path)
    assert [r.model for r in reversed(db.list_runs())] == ["m1", "m2"]
    assert load_benchmark(BENCH)  # the shipped file is untouched


def test_eval_refuses_too_many_runs(tmp_path):
    code, out, err = run_cli(eval_args(tmp_path, "--max-runs", "2"), eval_deps({}))
    assert code == 5 and "max-runs" in err
    assert not (tmp_path / "evalout").exists()  # nothing ran


def test_eval_bad_inputs(tmp_path):
    argv = eval_args(tmp_path)
    argv[argv.index("--issues") + 1] = str(tmp_path / "nope.jsonl")
    code, _, err = run_cli(argv, eval_deps({}))
    assert code == 5 and "not found" in err
    code, _, err = run_cli(eval_args(tmp_path, models=" , "), eval_deps({}))
    assert code == 5 and "--models" in err


def test_eval_needs_prices_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("PRICE_IN_PER_M")
    monkeypatch.delenv("PRICE_OUT_PER_M")
    code, _, err = run_cli(eval_args(tmp_path, models="test-model"), eval_deps({}))
    assert code == 5 and "test-model" in err and "PRICE_IN_PER_M" in err


def test_eval_never_costs_another_model_at_gmi_model_prices(tmp_path):
    # PRICE_* are set for GMI_MODEL, but "m3" has no MODEL_PRICES entry: refused, nothing runs.
    code, _, err = run_cli(eval_args(tmp_path, models="m1,m3"), eval_deps({}))
    assert code == 5 and "No prices for m3," in err and "MODEL_PRICES" in err
    assert not (tmp_path / "evalout").exists()


def test_eval_costs_each_model_at_its_own_price(tmp_path):
    from tests.test_evals import BENCH

    one = tmp_path / "one.jsonl"
    one.write_text(BENCH.read_text().splitlines()[0] + "\n")
    argv = eval_args(tmp_path)
    argv[argv.index(str(BENCH))] = str(one)
    assert run_cli(argv, eval_deps({}))[0] == 0
    results = json.loads((tmp_path / "evalout" / "results.json").read_text())
    cost = {r["model"]: r["cost"] for r in results}
    assert cost == {"m1": "0.0018", "m2": "0.0009"}  # 300 in, 150 out at 2/8 and 1/4


def test_compare_prints_saved_results(tmp_path):
    from tests.test_evals import BENCH

    one = tmp_path / "one.jsonl"
    one.write_text(BENCH.read_text().splitlines()[0] + "\n")
    argv = eval_args(tmp_path, models="m1")
    argv[argv.index(str(BENCH))] = str(one)
    assert run_cli(argv, eval_deps({}))[0] == 0
    code, out, _ = run_cli(["compare", str(tmp_path / "evalout")], make_deps())
    assert code == 0 and "| m1 |" in out and "100.0%" in out


def test_compare_missing_results(tmp_path):
    code, _, err = run_cli(["compare", str(tmp_path / "nothing")], make_deps())
    assert code == 5 and "results" in err


# --- approve / reject ----------------------------------------------------------------

WRITE_TOKEN = "github_pat_very_secret_write_token"
PR_URL = "https://github.com/octo/demo/pull/9"


class FakeWriter:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def create_draft_pr(self, owner, repo, branch, base_sha, files, title, body):
        self.calls.append(dict(owner=owner, repo=repo, branch=branch, base_sha=base_sha,
                               files=files, title=title, body=body))
        if self.error:
            raise self.error
        return PR_URL


def approve_deps(writer, answer="yes"):
    deps = make_deps()
    deps.writer = lambda token: writer
    deps.ask = lambda prompt: answer
    return deps


@pytest.fixture
def passed_run(source, issue_file, monkeypatch):
    monkeypatch.setenv("GITHUB_WRITE_TOKEN", WRITE_TOKEN)
    code, _, _ = run_cli(args(source, issue_file), good_run_deps())
    assert code == 0
    return 1


def test_approve_creates_a_draft_pr_from_the_tested_working_copy(passed_run, source, tmp_path):
    writer = FakeWriter()
    code, out, err = run_cli(["approve", "1", "--yes", "--note", "ok"], approve_deps(writer))
    assert code == 0 and PR_URL in out
    (call,) = writer.calls
    assert (call["owner"], call["repo"]) == ("octo", "demo")
    assert call["branch"] == "repopilot/issue-7-run-1"
    assert call["base_sha"] == git(source, "rev-parse", "HEAD")
    assert [f.path for f in call["files"]] == ["shop/cart.py"]
    assert b"if not prices" in call["files"][0].content
    assert call["body"].startswith("Refs #7") and "Status: PASSED" in call["body"]
    assert call["title"].startswith("Fix #7:")
    db = db_of(tmp_path)
    assert db.get_run(1).pr_url == PR_URL
    assert [a["decision"] for a in db.get_approvals(1)] == ["approved"]
    assert WRITE_TOKEN not in out + err


def test_closes_issue_flag_uses_fixes(passed_run):
    writer = FakeWriter()
    run_cli(["approve", "1", "--yes", "--closes-issue"], approve_deps(writer))
    assert writer.calls[0]["body"].startswith("Fixes #7")


@pytest.mark.parametrize("answer", ["no", "", "y", "YES please"])
def test_anything_but_yes_at_the_prompt_sends_nothing(passed_run, tmp_path, answer):
    writer = FakeWriter()
    code, out, _ = run_cli(["approve", "1"], approve_deps(writer, answer))
    assert code == 1 and "Nothing was sent" in out
    assert writer.calls == [] and db_of(tmp_path).get_approvals(1) == []


def test_typing_yes_at_the_prompt_approves(passed_run):
    writer = FakeWriter()
    code, out, _ = run_cli(["approve", "1"], approve_deps(writer, " yes "))
    assert code == 0 and len(writer.calls) == 1
    assert "octo/demo" in out and "shop/cart.py" in out  # shown before asking


def test_reject_records_it_and_blocks_a_later_approve(passed_run, tmp_path):
    writer = FakeWriter()
    code, out, _ = run_cli(["reject", "1", "--note", "wrong fix"], approve_deps(writer))
    assert code == 0 and "rejected" in out
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "rejected" in err
    assert writer.calls == []
    assert [a["decision"] for a in db_of(tmp_path).get_approvals(1)] == ["rejected"]


def test_only_passed_runs_can_be_approved(passed_run, tmp_path):
    db = db_of(tmp_path)
    db._conn.execute("UPDATE runs SET status='failed' WHERE id=1")
    db._conn.commit()
    writer = FakeWriter()
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "passed" in err and writer.calls == []


def test_a_run_gets_at_most_one_pr(passed_run):
    writer = FakeWriter()
    run_cli(["approve", "1", "--yes"], approve_deps(writer))
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and PR_URL in err and len(writer.calls) == 1
    code, _, err = run_cli(["reject", "1"], approve_deps(writer))
    assert code == 5 and "already has a draft PR" in err


def test_missing_write_token_is_refused(passed_run, monkeypatch):
    monkeypatch.delenv("GITHUB_WRITE_TOKEN")
    writer = FakeWriter()
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "GITHUB_WRITE_TOKEN" in err and writer.calls == []


def test_removed_working_copy_is_refused(passed_run, tmp_path):
    shutil.rmtree(db_of(tmp_path).get_run(1).workdir)
    writer = FakeWriter()
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "Working copy" in err and writer.calls == []


def test_working_copy_edited_after_the_tests_is_refused(passed_run, tmp_path):
    workdir = Path(db_of(tmp_path).get_run(1).workdir)
    (workdir / "shop" / "cart.py").write_text("print('not what was tested')\n")
    writer = FakeWriter()
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "uncommitted" in err and writer.calls == []


def test_run_without_a_github_repo_is_refused(source, issue_file, monkeypatch):
    monkeypatch.setenv("GITHUB_WRITE_TOKEN", WRITE_TOKEN)
    git(source, "remote", "remove", "origin")
    run_cli(args(source, issue_file), good_run_deps())
    writer = FakeWriter()
    code, _, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "GitHub repo" in err and writer.calls == []


def test_github_failure_is_reported_and_no_pr_is_saved(passed_run, tmp_path):
    from app.pr import PRError

    writer = FakeWriter(PRError("Creating the pull request failed (422); branch was deleted"))
    code, out, err = run_cli(["approve", "1", "--yes"], approve_deps(writer))
    assert code == 5 and "422" in err and WRITE_TOKEN not in out + err
    assert db_of(tmp_path).get_run(1).pr_url is None


def test_unknown_run_for_approve_and_reject():
    for cmd in ("approve", "reject"):
        code, _, err = run_cli([cmd, "42", "--yes"] if cmd == "approve" else [cmd, "42"],
                               approve_deps(FakeWriter()))
        assert code == 5 and "No run" in err


def test_show_includes_the_pr_link(passed_run):
    run_cli(["approve", "1", "--yes"], approve_deps(FakeWriter()))
    _, out, _ = run_cli(["show", "1"], make_deps())
    assert f"Draft PR: {PR_URL}" in out
