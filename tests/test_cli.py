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
    code, _, err = run_cli(eval_args(tmp_path, models="m1"), eval_deps({}))
    assert code == 5 and "PRICE_IN_PER_M" in err


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
