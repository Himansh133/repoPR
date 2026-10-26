"""A full run: baseline tests, investigate, patch, test, repair (max 2), honest final status.

The backend owns the workflow. The model can ask for `search_code`, `read_file` and
`propose_patch`; it can never run tests or commands. After every applied patch the backend
runs the approved test profile in the sandbox and decides what happens next.

Final status:
  passed        the tests failed before the patch and all pass after it
  failed        tests still fail after the allowed repair attempts
  inconclusive  tests passed but were already passing, or no tests ran
  aborted       sandbox violation, budget, LLM error or too many invalid calls
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.events import AgentEvent, EventLog
from app.agent.investigate import LLM, Budgets, Status, run_investigation
from app.agent.messages import assistant_message
from app.agent.plan import Plan
from app.agent.prompts import (
    NO_TOOL_NUDGE,
    PATCH_SYSTEM_PROMPT,
    patch_user_prompt,
    repair_feedback,
)
from app.agent.states import State, StateMachine
from app.config import Settings
from app.github import Issue
from app.llm import LLMError
from app.patch import PatchError, PatchResult, WorkingCopy
from app.sandbox import ExecStatus, Execution, Sandbox, SandboxError
from app.tools.dispatcher import Dispatcher
from app.tools.schemas import function_spec, tool_specs
from app.workspace import Workspace

PROPOSE_PATCH = "propose_patch"
MAX_PATCH_ARGS_BYTES = 30_000
PROPOSE_PATCH_DESCRIPTION = (
    "Propose a minimal unified diff. The backend validates and applies it, then runs the tests."
)


class ProposePatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diff: str = Field(min_length=1, max_length=20_000, description="A unified diff.")


class RunStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    ABORTED = "aborted"


@dataclass(frozen=True)
class RunBudgets:
    """Limits for the whole run. The investigation phase gets its own smaller share."""

    max_steps: int = 24
    max_tokens: int = 60_000
    timeout_s: float = 300
    max_invalid_calls: int = 3
    max_repairs: int = 2
    investigation_steps: int = 12
    investigation_tokens: int = 30_000

    @classmethod
    def from_settings(cls, s: Settings) -> "RunBudgets":
        return cls(
            max_steps=s.agent_run_max_steps,
            max_tokens=s.agent_run_max_tokens,
            timeout_s=s.agent_run_timeout_s,
            max_invalid_calls=s.agent_max_invalid_calls,
            max_repairs=s.agent_max_repairs,
            investigation_steps=s.agent_max_steps,
            investigation_tokens=s.agent_max_total_tokens,
        )


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    reason: str | None
    state: State
    plan: Plan | None
    final_diff: str
    branch: str
    workdir: Path  # the working copy, kept so the caller can inspect or open a PR from it
    baseline: Execution | None
    executions: list[Execution]  # test runs after each patch
    patches: list[PatchResult]
    repairs_used: int
    files_read: list[str]
    steps: int
    input_tokens: int
    output_tokens: int
    events: list[AgentEvent] = field(default_factory=list)


async def run_issue(
    llm: LLM,
    source: str | Path,
    issue: Issue,
    sandbox: Sandbox,
    budgets: RunBudgets | None = None,
    clock: Callable[[], float] = time.monotonic,
    profile: str = "python-pytest",
    work_dir: Path | None = None,
) -> RunResult:
    """Run the whole workflow on a clone of `source`. Raises PatchError if the clone fails.

    The working copy is left in place (`result.workdir`); the caller removes it.
    """
    started = clock()
    wc = WorkingCopy.create(source, issue.number, work_dir)
    run = _Run(llm, wc, issue, sandbox, budgets or RunBudgets(), clock, profile, started)
    return await run.execute()


class _Run:
    def __init__(self, llm, wc, issue, sandbox, budgets, clock, profile, started) -> None:
        self.llm = llm
        self.wc = wc
        self.issue = issue
        self.sandbox = sandbox
        self.b = budgets
        self.clock = clock
        self.profile = profile
        self.started = started
        self.events = EventLog()
        self.machine = StateMachine(self.events)
        self.dispatcher = Dispatcher(Workspace(wc.root))
        self.steps = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.invalid = 0
        self.baseline: Execution | None = None
        self.executions: list[Execution] = []
        self.patches: list[PatchResult] = []
        self.repairs_used = 0
        self.plan: Plan | None = None
        self.messages: list[dict] = []

    # --- the workflow ------------------------------------------------------------------

    async def execute(self) -> RunResult:
        self.machine.transition(State.INVESTIGATE, "start")

        try:
            self.baseline = self.sandbox.run_tests(self.profile, self.wc.root)
        except SandboxError as exc:
            return self._abort(f"sandbox error: {exc}")
        self._record_tests("baseline", self.baseline)
        if self.baseline.status is ExecStatus.VIOLATION:
            return self._abort(self.baseline.reason or "sandbox violation")

        inv = await run_investigation(
            self.llm,
            self.dispatcher,
            self.issue,
            Budgets(
                max_steps=min(self.b.investigation_steps, self.b.max_steps),
                max_tokens=min(self.b.investigation_tokens, self.b.max_tokens),
                timeout_s=max(0.0, self.b.timeout_s - (self.clock() - self.started)),
                max_invalid_calls=self.b.max_invalid_calls,
            ),
            self.clock,
            events=self.events,
            machine=self.machine,
        )
        self.steps += inv.steps
        self.in_tokens += inv.input_tokens
        self.out_tokens += inv.output_tokens
        if inv.status is not Status.PLAN_READY:
            detail = f": {inv.error}" if inv.error else ""
            return self._abort(f"investigation stopped ({inv.status.value}){detail}")
        self.plan = inv.plan

        self.machine.transition(State.PATCH, "plan accepted")
        self.messages = [
            {"role": "system", "content": PATCH_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": patch_user_prompt(self.issue, self.plan, self.baseline.failed_tests),
            },
        ]
        self.tools = tool_specs(self.dispatcher.allowed) + [
            function_spec(PROPOSE_PATCH, PROPOSE_PATCH_DESCRIPTION, ProposePatchArgs)
        ]
        return await self._patch_and_test()

    async def _patch_and_test(self) -> RunResult:
        origin = State.PATCH
        while True:
            outcome = await self._request_patch(origin)
            if isinstance(outcome, str):
                return self._abort(outcome)
            patch, call_id = outcome

            self.machine.transition(State.TEST, "patch applied")
            try:
                execution = self.sandbox.run_tests(self.profile, self.wc.root)
            except SandboxError as exc:
                return self._abort(f"sandbox error: {exc}")
            self.executions.append(execution)
            self._record_tests("tests_run", execution)

            if execution.status is ExecStatus.VIOLATION:
                return self._abort(execution.reason or "sandbox violation")
            if execution.status is ExecStatus.PASSED:
                return self._report(*self._judge_passed())
            if execution.status is ExecStatus.INCONCLUSIVE:
                return self._report(
                    RunStatus.INCONCLUSIVE, execution.reason or "the tests were inconclusive"
                )
            # FAILED
            if self.repairs_used >= self.b.max_repairs:
                return self._report(
                    RunStatus.FAILED,
                    f"tests still fail after {self.repairs_used} repair attempts",
                )
            self.repairs_used += 1
            self.machine.transition(State.REPAIR, "tests failed")
            self.events.record("repair_started", state="REPAIR", attempt=self.repairs_used)
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": repair_feedback(
                        patch.commit_sha,
                        execution.failed_tests,
                        execution.log_tail,
                        self.repairs_used,
                        self.b.max_repairs,
                    ),
                }
            )
            origin = State.REPAIR

    def _judge_passed(self) -> tuple[RunStatus, str | None]:
        if self.baseline is not None and self.baseline.status is ExecStatus.FAILED:
            return RunStatus.PASSED, None
        return RunStatus.INCONCLUSIVE, (
            "the tests pass, but they were already passing before the patch, "
            "so the patch is not shown to fix anything"
        )

    # --- one model turn loop until a patch is applied --------------------------------

    def _budget_reason(self) -> str | None:
        if self.steps >= self.b.max_steps:
            return "step limit reached"
        if self.in_tokens + self.out_tokens >= self.b.max_tokens:
            return "token budget reached"
        if self.clock() - self.started >= self.b.timeout_s:
            return "timeout: the run exceeded its time limit"
        return None

    async def _request_patch(self, origin: State) -> str | tuple[PatchResult, str]:
        """Talk to the model until a patch is applied. Returns it, or a reason to abort."""
        while True:
            reason = self._budget_reason()
            if reason:
                self.events.record("budget", state=self.machine.state.value, reason=reason)
                return reason
            try:
                result = await self.llm.complete(self.messages, self.tools)
            except LLMError as exc:
                self.events.record(
                    "llm_error", state=self.machine.state.value, error_type=type(exc).__name__
                )
                return f"LLM error: {type(exc).__name__}: {exc}"
            self.steps += 1
            self.in_tokens += result.input_tokens
            self.out_tokens += result.output_tokens
            self.events.record(
                "llm_call",
                state=self.machine.state.value,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tool_calls=len(result.tool_calls),
            )

            if not result.tool_calls:
                self.messages.append({"role": "assistant", "content": result.text})
                self.messages.append({"role": "user", "content": NO_TOOL_NUDGE})
                self.events.record("no_tool_call", state=self.machine.state.value)
                self.invalid += 1
                if self.invalid >= self.b.max_invalid_calls:
                    return "too many invalid calls in a row"
                continue

            self.messages.append(assistant_message(result))
            calls = result.tool_calls
            for index, call in enumerate(calls):
                if call.name == PROPOSE_PATCH:
                    patch, error = self._try_patch(call.arguments, origin)
                    if patch is not None:
                        for skipped in calls[index + 1 :]:
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": skipped.id,
                                    "content": "Skipped: only one patch is handled at a time.",
                                }
                            )
                        self.invalid = 0
                        return patch, call.id
                    ok, content = False, error
                else:
                    self.events.record(
                        "tool_call",
                        state=self.machine.state.value,
                        name=call.name,
                        args_bytes=len(call.arguments.encode()),
                    )
                    outcome = self.dispatcher.dispatch(call)
                    self.events.record(
                        "tool_result",
                        state=self.machine.state.value,
                        name=call.name,
                        ok=outcome.ok,
                        chars=len(outcome.content),
                    )
                    ok, content = outcome.ok, outcome.content
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                self.invalid = 0 if ok else self.invalid + 1
                if self.invalid >= self.b.max_invalid_calls:
                    return "too many invalid calls in a row"

    def _try_patch(self, raw: str, origin: State) -> tuple[PatchResult | None, str]:
        """Validate and apply a proposed patch. Returns (result, "") or (None, message)."""
        self.machine.transition(State.VALIDATE, "propose_patch")
        problem = None
        patch = None
        if len(raw.encode()) > MAX_PATCH_ARGS_BYTES:
            problem = "Error: Patch arguments are too large"
        else:
            try:
                data = json.loads(raw)
                args = ProposePatchArgs(**data) if isinstance(data, dict) else None
            except (ValueError, ValidationError, TypeError):
                args = None
            if args is None:
                problem = 'Error: Arguments must be a JSON object like {"diff": "<unified diff>"}'
            else:
                try:
                    patch = self.wc.apply_patch(args.diff)
                except PatchError as exc:
                    problem = f"Error: {exc}"
        if patch is None:
            self.machine.transition(origin, "patch rejected")
            self.events.record("patch_rejected", state=origin.value, reason=problem)
            return None, problem or "Error: patch rejected"
        self.patches.append(patch)
        self.events.record(
            "patch_proposed",
            state="VALIDATE",
            files=len(patch.files),
            added=patch.added,
            removed=patch.removed,
        )
        return patch, ""

    # --- finishing -----------------------------------------------------------------------

    def _record_tests(self, kind: str, execution: Execution) -> None:
        self.events.record(
            kind,
            state=self.machine.state.value,
            status=execution.status.value,
            exit_code=execution.exit_code,
            failed=len(execution.failed_tests),
            duration_s=round(execution.duration_s, 2),
        )

    def _abort(self, reason: str) -> RunResult:
        self.events.record("abort", state=self.machine.state.value, reason=reason)
        if self.machine.state not in (State.ABORTED, State.REPORT):
            self.machine.transition(State.ABORTED, reason[:100])
        return self._result(RunStatus.ABORTED, reason)

    def _report(self, status: RunStatus, reason: str | None) -> RunResult:
        self.machine.transition(State.REPORT, status.value)
        self.events.record("report", state="REPORT", status=status.value)
        return self._result(status, reason)

    def _result(self, status: RunStatus, reason: str | None) -> RunResult:
        try:
            diff = self.wc.final_diff() if self.patches else ""
        except PatchError:
            diff = ""
        return RunResult(
            status=status,
            reason=reason,
            state=self.machine.state,
            plan=self.plan,
            final_diff=diff,
            branch=self.wc.branch,
            workdir=self.wc.root,
            baseline=self.baseline,
            executions=self.executions,
            patches=self.patches,
            repairs_used=self.repairs_used,
            files_read=sorted(self.dispatcher.files_read),
            steps=self.steps,
            input_tokens=self.in_tokens,
            output_tokens=self.out_tokens,
            events=self.events.events,
        )
