"""Tests for the plain-text report. Results are built by hand."""

from pathlib import Path

from app.agent.fix import RunResult, RunStatus
from app.agent.plan import CitedFile, Plan
from app.agent.states import State
from app.github import Issue
from app.patch import PatchResult
from app.report import build_report
from app.sandbox import ExecStatus
from tests.fakes import execution

ISSUE = Issue(number=7, title="Crash on empty cart", body="b", state="open", url="u", author="a")
PLAN = Plan(
    summary="Guard against empty lists.",
    cited_files=[CitedFile(path="shop/cart.py", reason="holds the bug")],
    steps=["Return 0 when empty."],
)
PATCH = PatchResult(
    files=["shop/cart.py"], added=2, removed=0, branch="repopilot/issue-7",
    commit_sha="abc123", diff="",
)
DIFF = "diff --git a/shop/cart.py b/shop/cart.py\n+    if not prices:\n"
FAIL = "tests/test_cart.py::test_empty"


def result(status, reason=None, **kw):
    base = dict(
        status=status, reason=reason, state=State.REPORT, plan=PLAN, final_diff=DIFF,
        branch="repopilot/issue-7", workdir=Path("/x"),
        baseline=execution(ExecStatus.FAILED, [FAIL]),
        executions=[execution(ExecStatus.PASSED)], patches=[PATCH], repairs_used=0,
        files_read=["shop/cart.py"], steps=3, input_tokens=300, output_tokens=150, events=[],
    )
    base.update(kw)
    return RunResult(**base)


def sections(text):
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_passed_report():
    text = build_report(result(RunStatus.PASSED), ISSUE)
    assert "Issue #7: Crash on empty cart" in text
    assert "Status: PASSED" in text
    assert sections(text) == [
        "## Plan", "## Proposed changes", "## Test results", "## Unresolved", "## Usage",
    ]
    assert "shop/cart.py" in text and "+2 -0" in text and DIFF.strip() in text
    assert f"Before the patch: FAILED ({FAIL})" in text
    assert "After patch 1: PASSED" in text
    assert "Nothing unresolved" in text
    assert "300 in, 150 out" in text


def test_failed_report_lists_failures_and_unresolved():
    ex = execution(ExecStatus.FAILED, [FAIL, "tests/test_x.py::test_y"])
    text = build_report(
        result(RunStatus.FAILED, "tests still fail after 2 repair attempts",
               executions=[ex, ex, ex], repairs_used=2),
        ISSUE,
    )
    assert "Status: FAILED" in text
    assert "tests still fail after 2 repair attempts" in text
    assert "After patch 3: FAILED" in text
    assert "tests/test_x.py::test_y" in text
    assert "Still failing" in text
    assert "Repair attempts used: 2" in text


def test_inconclusive_report_says_so():
    text = build_report(
        result(RunStatus.INCONCLUSIVE, "tests were already passing before the patch",
               baseline=execution(ExecStatus.PASSED)),
        ISSUE,
    )
    assert "Status: INCONCLUSIVE" in text
    assert "already passing" in text
    assert "not shown to fix" in text


def test_aborted_report_without_a_patch():
    text = build_report(
        result(RunStatus.ABORTED, "timeout: tests exceeded the time limit", plan=None,
               final_diff="", executions=[], patches=[], state=State.ABORTED),
        ISSUE,
    )
    assert "Status: ABORTED" in text
    assert "timeout: tests exceeded the time limit" in text
    assert "No patch was produced" in text
    assert "## Plan" not in text
    assert "Run stopped early" in text


def test_report_never_claims_success_for_non_passed():
    for status in (RunStatus.FAILED, RunStatus.INCONCLUSIVE, RunStatus.ABORTED):
        text = build_report(result(status, "why"), ISSUE)
        assert "Status: PASSED" not in text


def test_long_diff_is_bounded():
    text = build_report(result(RunStatus.PASSED, final_diff="+x\n" * 20_000), ISSUE)
    assert len(text) < 20_000
    assert "diff truncated" in text
