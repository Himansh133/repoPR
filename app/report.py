"""A plain-text report. Keeps proposed changes, test results and unresolved items apart.

The report states only what the backend measured. It never says "it works" unless the
final status is `passed`.
"""

from app.agent.fix import RunResult, RunStatus
from app.github import Issue
from app.sandbox import Execution

MAX_DIFF_CHARS = 8000


def _line(label: str, execution: Execution | None) -> str:
    if execution is None:
        return f"{label}: not run"
    names = f" ({', '.join(execution.failed_tests)})" if execution.failed_tests else ""
    extra = f" - {execution.reason}" if execution.reason else ""
    return f"{label}: {execution.status.value.upper()}{names}{extra}"


def build_report(result: RunResult, issue: Issue) -> str:
    """Build the final report for a run."""
    out = [f"RepoPilot report - Issue #{issue.number}: {issue.title}", ""]
    out.append(f"Status: {result.status.value.upper()}")
    if result.reason:
        out.append(f"Reason: {result.reason}")

    if result.plan is not None:
        out += ["", "## Plan", result.plan.summary]
        out += [f"- {c.path}: {c.reason}" for c in result.plan.cited_files]
        out += [f"{i}. {step}" for i, step in enumerate(result.plan.steps, 1)]

    out += ["", "## Proposed changes"]
    if result.patches:
        added = sum(p.added for p in result.patches)
        removed = sum(p.removed for p in result.patches)
        files = sorted({f for p in result.patches for f in p.files})
        count = len(result.patches)
        out.append(f"Branch: {result.branch} ({count} patch(es), +{added} -{removed})")
        out += [f"- {f}" for f in files]
        diff = result.final_diff
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n[diff truncated]"
        out += ["```diff", diff.strip(), "```"]
    else:
        out.append("No patch was produced.")

    out += ["", "## Test results", _line("Before the patch", result.baseline)]
    out += [_line(f"After patch {i}", e) for i, e in enumerate(result.executions, 1)]
    out.append(f"Repair attempts used: {result.repairs_used}")

    out += ["", "## Unresolved"]
    final = result.executions[-1] if result.executions else None
    if result.status is RunStatus.PASSED:
        out.append("Nothing unresolved: the tests failed before the patch and pass after it.")
    elif result.status is RunStatus.FAILED:
        names = ", ".join(final.failed_tests) if final and final.failed_tests else "see test log"
        out.append(f"Still failing: {names}")
    elif result.status is RunStatus.INCONCLUSIVE:
        out.append("The result is inconclusive, so the patch is not shown to fix the issue.")
    else:
        out.append("Run stopped early; the issue is not resolved.")

    out += [
        "",
        "## Usage",
        f"Steps: {result.steps}. Tokens: {result.input_tokens} in, {result.output_tokens} out.",
    ]
    return "\n".join(out) + "\n"
