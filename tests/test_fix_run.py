"""End-to-end agent runs (investigate, patch, test, repair, report) with a fake LLM and sandbox.

The working copy is a real git clone of the fixture repo; only the LLM and Docker are fake.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent.fix import RunBudgets, RunResult, RunStatus, run_issue
from app.agent.states import State
from app.config import Settings
from app.github import Issue
from app.llm import LLMRateLimitError
from app.patch import PatchError
from app.sandbox import ExecStatus, SandboxError
from tests.fakes import FakeLLM, FakeSandbox, execution, make_diff, plan_args, reply, tool_call

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
CART = (FIXTURE / "shop" / "cart.py").read_text()
BUG = "    return sum(prices) / len(prices)"
WRONG = CART.replace(BUG, "    if not prices:\n        return 1\n" + BUG)  # not the right fix
RIGHT = CART.replace(BUG, "    if not prices:\n        return 0\n" + BUG)
FAIL_NAME = "tests/test_cart.py::test_average_price_of_empty_cart_is_zero"

ISSUE = Issue(
    number=7,
    title="average_price crashes on an empty cart",
    body="average_price([]) raises ZeroDivisionError. It should return 0.",
    state="open",
    url="https://github.com/o/r/issues/7",
    author="alice",
    labels=["bug"],
)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def no_rg(monkeypatch):
    monkeypatch.setattr("app.tools.search.shutil.which", lambda name: None)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def investigate_script():
    return [
        reply(tool_call("read_file", {"path": "shop/cart.py"}, "r1")),
        reply(tool_call("submit_plan", plan_args(), "p1")),
    ]


def propose(old, new, id="pp1", path="shop/cart.py"):
    return reply(tool_call("propose_patch", {"diff": make_diff(path, old, new)}, id))


FAILING = execution(ExecStatus.FAILED, [FAIL_NAME], "E ZeroDivisionError: division by zero")
PASSING = execution(ExecStatus.PASSED, log="2 passed in 0.02s")


async def go(source, llm, sandbox, budgets=None, **kw):
    result = await run_issue(llm, source, ISSUE, sandbox, budgets or RunBudgets(), **kw)
    try:
        return result
    finally:
        if result.workdir:
            shutil.rmtree(result.workdir, ignore_errors=True)


# --- settings -----------------------------------------------------------------

def test_run_budgets_from_settings(monkeypatch):
    monkeypatch.setenv("GMI_API_KEY", "k")
    monkeypatch.setenv("GMI_MODEL", "m")
    monkeypatch.setenv("AGENT_MAX_REPAIRS", "1")
    b = RunBudgets.from_settings(Settings(_env_file=None))
    assert (b.max_steps, b.max_tokens, b.timeout_s, b.max_repairs) == (24, 150_000, 300, 1)


# --- passing run --------------------------------------------------------------

async def test_passes_on_the_first_try(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    sandbox = FakeSandbox(FAILING, PASSING)
    result = await go(source, llm, sandbox)
    assert isinstance(result, RunResult)
    assert result.status is RunStatus.PASSED
    assert result.state is State.REPORT
    assert result.repairs_used == 0
    assert result.plan.cited_files[0].path == "shop/cart.py"
    assert "+    if not prices:" in result.final_diff and "return 0" in result.final_diff
    assert result.branch == "repopilot/issue-7"
    assert [e.status for e in [result.baseline, *result.executions]] == [
        ExecStatus.FAILED, ExecStatus.PASSED,
    ]
    assert (result.steps, result.input_tokens, result.output_tokens) == (3, 300, 150)
    # Baseline ran on the unpatched copy; the second run saw the patched file.
    assert "if not prices" not in sandbox.calls[0]["cart"]
    assert "if not prices" in sandbox.calls[1]["cart"]
    assert sandbox.calls[0]["profile"] == "python-pytest"
    # The original checkout is untouched.
    assert (source / "shop" / "cart.py").read_text() == CART
    states = [(e.data["from"], e.data["to"]) for e in result.events if e.kind == "transition"]
    assert states == [
        ("INIT", "INVESTIGATE"), ("INVESTIGATE", "PLAN"), ("PLAN", "PATCH"),
        ("PATCH", "VALIDATE"), ("VALIDATE", "TEST"), ("TEST", "REPORT"),
    ]


async def test_auto_profile_is_detected_and_prepared_before_the_baseline(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    sandbox = FakeSandbox(FAILING, PASSING)
    result = await go(source, llm, sandbox, profile="auto")
    assert result.status is RunStatus.PASSED
    assert sandbox.prepared == ["python-pytest"]
    assert [c["profile"] for c in sandbox.calls] == ["python-pytest", "python-pytest"]


async def test_install_failure_aborts_before_any_model_call(source):
    sandbox = FakeSandbox()
    sandbox.prepare = lambda profile, workdir: (_ for _ in ()).throw(
        SandboxError("dependency install failed (exit 1): 404")
    )
    llm = FakeLLM([])
    result = await go(source, llm, sandbox)
    assert result.status is RunStatus.ABORTED
    assert "install failed" in result.reason
    assert llm.calls == []


# --- repair -------------------------------------------------------------------

async def test_events_are_streamed_to_a_listener_as_they_happen(source):
    seen = []
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await run_issue(llm, source, ISSUE, FakeSandbox(FAILING, PASSING),
                             on_event=seen.append)
    assert seen == result.events and seen[0].kind == "transition"
    shutil.rmtree(result.workdir, ignore_errors=True)


async def test_fail_then_repair_then_pass(source):
    big_log = "E noisy line\n" * 2000 + "E AssertionError: assert 1 == 0"
    llm = FakeLLM([*investigate_script(), propose(CART, WRONG), propose(WRONG, RIGHT, "pp2")])
    sandbox = FakeSandbox(
        FAILING, execution(ExecStatus.FAILED, [FAIL_NAME], big_log), PASSING
    )
    result = await go(source, llm, sandbox)
    assert result.status is RunStatus.PASSED
    assert result.repairs_used == 1
    assert len(result.patches) == 2
    assert "return 0" in result.final_diff and "return 1" not in result.final_diff
    # What the model saw after the failed run: failed names + bounded output, wrapped as untrusted.
    feedback = llm.calls[-1]["messages"][-1]
    assert feedback["role"] == "tool" and feedback["tool_call_id"] == "pp1"
    assert FAIL_NAME in feedback["content"]
    assert "AssertionError: assert 1 == 0" in feedback["content"]
    assert "<untrusted_test_output>" in feedback["content"]
    assert "Repair attempt 1 of 2" in feedback["content"]
    assert len(feedback["content"]) < 4_500
    assert "FAILED" in feedback["content"]
    transitions = [(e.data["from"], e.data["to"]) for e in result.events if e.kind == "transition"]
    assert ("TEST", "REPAIR") in transitions and ("REPAIR", "VALIDATE") in transitions


async def test_fails_after_two_repairs(source):
    p1, p2, p3 = "return 1", "return 2", "return 3"
    v1 = WRONG
    v2 = WRONG.replace(p1, p2)
    v3 = WRONG.replace(p1, p3)
    llm = FakeLLM(
        [*investigate_script(), propose(CART, v1), propose(v1, v2, "pp2"), propose(v2, v3, "pp3")]
    )
    sandbox = FakeSandbox(FAILING, FAILING, FAILING, FAILING)
    result = await go(source, llm, sandbox)
    assert result.status is RunStatus.FAILED
    assert result.state is State.REPORT
    assert result.repairs_used == 2
    assert len(result.patches) == 3
    assert len(sandbox.calls) == 4  # baseline + 3 patches, never a 4th patch
    assert FAIL_NAME in result.executions[-1].failed_tests
    assert "2 repair" in result.reason


# --- aborts -------------------------------------------------------------------

async def test_sandbox_violation_aborts_immediately(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT), propose(RIGHT, RIGHT)])
    sandbox = FakeSandbox(
        FAILING, execution(ExecStatus.VIOLATION, reason="timeout: tests exceeded the time limit")
    )
    result = await go(source, llm, sandbox)
    assert result.status is RunStatus.ABORTED
    assert result.state is State.ABORTED
    assert "timeout" in result.reason
    assert len(llm.calls) == 3  # no further model calls
    assert len(sandbox.calls) == 2


async def test_baseline_violation_aborts_before_any_model_call(source):
    llm = FakeLLM([])
    sandbox = FakeSandbox(execution(ExecStatus.VIOLATION, reason="container was killed"))
    result = await go(source, llm, sandbox)
    assert result.status is RunStatus.ABORTED and llm.calls == []
    assert "killed" in result.reason


async def test_sandbox_error_aborts(source):
    result = await go(source, FakeLLM([]), FakeSandbox(SandboxError("Docker is not installed")))
    assert result.status is RunStatus.ABORTED
    assert "Docker is not installed" in result.reason


async def test_sandbox_error_after_patch_aborts(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(FAILING, SandboxError("daemon went away")))
    assert result.status is RunStatus.ABORTED and "daemon went away" in result.reason


async def test_investigation_failure_aborts(source):
    llm = FakeLLM([reply(tool_call("nope", {}, f"b{i}")) for i in range(3)])
    result = await go(source, llm, FakeSandbox(FAILING))
    assert result.status is RunStatus.ABORTED
    assert result.patches == [] and result.final_diff == ""
    assert result.state is State.ABORTED


async def test_llm_error_in_patch_phase_aborts(source):
    llm = FakeLLM([*investigate_script(), LLMRateLimitError("Rate limited (429)")])
    result = await go(source, llm, FakeSandbox(FAILING))
    assert result.status is RunStatus.ABORTED and "LLMRateLimitError" in result.reason


async def test_step_budget_is_shared_across_phases(source):
    llm = FakeLLM(
        [*investigate_script(), reply(tool_call("read_file", {"path": "README.md"}, "r2")),
         reply(tool_call("read_file", {"path": "README.md"}, "r3"))]
    )
    result = await go(source, llm, FakeSandbox(FAILING), RunBudgets(max_steps=3))
    assert result.status is RunStatus.ABORTED and "step" in result.reason
    assert len(llm.calls) == 3


async def test_token_budget_is_shared_across_phases(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(FAILING), RunBudgets(max_tokens=250))
    assert result.status is RunStatus.ABORTED and "token" in result.reason
    assert len(llm.calls) == 2


async def test_timeout_during_repair_loop(source):
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()

    def advance(call_number):
        if call_number == 2:
            clock.t += 1000

    llm = FakeLLM([*investigate_script(), propose(CART, WRONG), propose(WRONG, RIGHT, "pp2")])
    sandbox = FakeSandbox(FAILING, FAILING, on_call=advance)
    result = await go(source, llm, sandbox, RunBudgets(timeout_s=60), clock=clock)
    assert result.status is RunStatus.ABORTED and "timeout" in result.reason
    assert len(llm.calls) == 3


# --- honest statuses ----------------------------------------------------------

async def test_baseline_already_passing_is_inconclusive(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(PASSING, PASSING))
    assert result.status is RunStatus.INCONCLUSIVE
    assert result.state is State.REPORT
    assert "already passing" in result.reason


async def test_no_tests_collected_is_inconclusive(source):
    none = execution(ExecStatus.INCONCLUSIVE, reason="no tests were collected")
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(none, none))
    assert result.status is RunStatus.INCONCLUSIVE
    assert "no tests" in result.reason


async def test_patch_that_removes_all_tests_is_inconclusive_not_passed(source):
    none = execution(ExecStatus.INCONCLUSIVE, reason="no tests were collected")
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(FAILING, none))
    assert result.status is RunStatus.INCONCLUSIVE


# --- the model's side ---------------------------------------------------------

async def test_invalid_patch_goes_back_to_the_model(source):
    bad = reply(tool_call("propose_patch", {"diff": make_diff("../evil.py", "a\n", "b\n")}, "bad"))
    llm = FakeLLM([*investigate_script(), bad, propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(FAILING, PASSING))
    assert result.status is RunStatus.PASSED
    rejection = llm.calls[-1]["messages"][-1]
    assert rejection["tool_call_id"] == "bad" and rejection["content"].startswith("Error:")
    assert any(e.kind == "patch_rejected" for e in result.events)
    transitions = [(e.data["from"], e.data["to"]) for e in result.events if e.kind == "transition"]
    assert ("VALIDATE", "PATCH") in transitions
    assert len(result.patches) == 1


async def test_three_invalid_patches_in_a_row_abort(source):
    bad = [
        reply(tool_call("propose_patch", {"diff": "nonsense"}, f"b{i}")) for i in range(3)
    ]
    llm = FakeLLM([*investigate_script(), *bad])
    result = await go(source, llm, FakeSandbox(FAILING))
    assert result.status is RunStatus.ABORTED and "invalid" in result.reason
    assert result.patches == []


async def test_patch_that_does_not_apply_is_rejected_cleanly(source):
    doesnt_fit = make_diff("shop/cart.py", CART.replace("average", "mean"), RIGHT)
    llm = FakeLLM(
        [*investigate_script(), reply(tool_call("propose_patch", {"diff": doesnt_fit}, "x")),
         propose(CART, RIGHT)]
    )
    result = await go(source, llm, FakeSandbox(FAILING, PASSING))
    assert result.status is RunStatus.PASSED
    assert "does not apply" in llm.calls[-1]["messages"][-1]["content"]


async def test_model_cannot_run_tests_or_shell(source):
    sneaky = reply(
        tool_call("run_tests", {"profile": "python-pytest"}, "t1"),
        tool_call("run_shell", {"cmd": "pytest"}, "t2"),
    )
    llm = FakeLLM([*investigate_script(), sneaky, propose(CART, RIGHT)])
    sandbox = FakeSandbox(FAILING, PASSING)
    result = await go(source, llm, sandbox)
    assert result.status is RunStatus.PASSED
    assert len(sandbox.calls) == 2  # only the backend's own runs
    assert "Unknown tool" in llm.calls[-1]["messages"][-1]["content"]


async def test_extra_tool_calls_after_a_patch_get_placeholder_answers(source):
    diff = make_diff("shop/cart.py", CART, RIGHT)
    batch = reply(
        tool_call("propose_patch", {"diff": diff}, "pp1"),
        tool_call("read_file", {"path": "README.md"}, "extra"),
    )
    other = RIGHT.replace("return 0", "return 0.0")
    llm = FakeLLM([*investigate_script(), batch, propose(RIGHT, other, "pp2")])
    result = await go(source, llm, FakeSandbox(FAILING, FAILING, PASSING))
    assert result.status is RunStatus.PASSED
    tool_msgs = [m for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    ids = [m["tool_call_id"] for m in tool_msgs]
    assert "pp1" in ids and "extra" in ids
    assert "Skipped" in next(m for m in tool_msgs if m["tool_call_id"] == "extra")["content"]


async def test_patch_prompt_has_plan_baseline_and_untrusted_markers(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    await go(source, llm, FakeSandbox(FAILING, PASSING))
    patch_call = llm.calls[2]
    system = patch_call["messages"][0]["content"]
    user = patch_call["messages"][1]["content"]
    assert "unified diff" in system and "untrusted" in system.lower()
    assert "do not claim" in system.lower() or "never claim" in system.lower()
    assert "<untrusted_issue>" in user and "ZeroDivisionError" in user
    assert "shop/cart.py" in user and "Guard against empty lists." in user
    assert FAIL_NAME in user
    names = [t["function"]["name"] for t in patch_call["tools"]]
    assert names == ["search_code", "read_file", "propose_patch"]


async def test_events_do_not_store_diffs_or_test_logs(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    sandbox = FakeSandbox(execution(ExecStatus.FAILED, [FAIL_NAME], "SECRET-LOG-LINE"), PASSING)
    result = await go(source, llm, sandbox)
    dump = " ".join(str(e.data) for e in result.events)
    assert "SECRET-LOG-LINE" not in dump
    assert "return sum(prices)" not in dump and "if not prices" not in dump
    kinds = {e.kind for e in result.events}
    assert {"baseline", "patch_proposed", "tests_run", "llm_call"} <= kinds


# --- cancel -------------------------------------------------------------------

async def test_cancel_before_the_baseline_runs_nothing(source):
    llm = FakeLLM([])
    sandbox = FakeSandbox()
    result = await go(source, llm, sandbox, should_stop=lambda: True)
    assert (result.status, result.reason) == (RunStatus.ABORTED, "cancelled")
    assert result.state is State.ABORTED
    assert sandbox.calls == [] and llm.calls == []
    assert [e.kind for e in result.events].count("cancelled") == 1


async def test_cancel_during_investigation_stops_at_the_next_step(source):
    llm = FakeLLM(investigate_script())
    sandbox = FakeSandbox(FAILING)
    result = await go(source, llm, sandbox, should_stop=lambda: len(llm.calls) >= 1)
    assert (result.status, result.reason) == (RunStatus.ABORTED, "cancelled")
    assert len(llm.calls) == 1 and len(sandbox.calls) == 1  # baseline only
    assert result.plan is None


async def test_cancel_in_the_patch_phase_stops_before_the_next_model_call(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    sandbox = FakeSandbox(FAILING, PASSING)
    result = await go(source, llm, sandbox, should_stop=lambda: len(llm.calls) >= 2)
    assert (result.status, result.reason) == (RunStatus.ABORTED, "cancelled")
    assert len(llm.calls) == 2 and result.patches == []


async def test_cancel_after_a_patch_skips_its_test_run(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    sandbox = FakeSandbox(FAILING, PASSING)
    result = await go(source, llm, sandbox, should_stop=lambda: len(llm.calls) >= 3)
    assert (result.status, result.reason) == (RunStatus.ABORTED, "cancelled")
    assert len(result.patches) == 1 and len(sandbox.calls) == 1


async def test_setup_failure_raises(tmp_path):
    with pytest.raises(PatchError):
        await run_issue(FakeLLM([]), tmp_path, ISSUE, FakeSandbox(), RunBudgets())


async def test_workdir_is_kept_for_the_caller(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await run_issue(llm, source, ISSUE, FakeSandbox(FAILING, PASSING), RunBudgets())
    try:
        assert result.workdir.is_dir()
        assert "if not prices" in (result.workdir / "shop" / "cart.py").read_text()
    finally:
        shutil.rmtree(result.workdir, ignore_errors=True)


async def test_llm_call_events_carry_latency_and_usage_flag(source):
    llm = FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    result = await go(source, llm, FakeSandbox(FAILING, PASSING))
    calls = [e for e in result.events if e.kind == "llm_call"]
    assert len(calls) == 3
    assert all("latency_s" in e.data and e.data["usage_missing"] is False for e in calls)
