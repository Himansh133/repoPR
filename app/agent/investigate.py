"""The investigation phase: read-only. The model searches and reads, then submits a plan.

The backend owns the workflow. The model can only request tools; the dispatcher validates
and runs them. A plan is accepted only if every file it cites was actually read.
The loop is finite: step limit, token budget, wall-clock timeout, and an abort after
too many invalid calls in a row.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import ValidationError

from app.agent.events import AgentEvent, EventLog
from app.agent.messages import assistant_message
from app.agent.plan import SUBMIT_PLAN_DESCRIPTION, Plan
from app.agent.prompts import NO_TOOL_NUDGE, SYSTEM_PROMPT, issue_prompt
from app.agent.states import State, StateMachine
from app.config import Settings
from app.github import Issue
from app.llm import LLMError, LLMResult
from app.tools.dispatcher import Dispatcher
from app.tools.schemas import function_spec, tool_specs

SUBMIT_PLAN = "submit_plan"
MAX_PLAN_ARGS_BYTES = 8192


class LLM(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult:
        ...


class Status(StrEnum):
    PLAN_READY = "plan_ready"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ABORTED = "aborted"
    LLM_ERROR = "llm_error"


@dataclass(frozen=True)
class Budgets:
    max_steps: int = 12
    max_tokens: int = 80_000
    timeout_s: float = 120
    max_invalid_calls: int = 3  # in a row

    @classmethod
    def from_settings(cls, s: Settings) -> "Budgets":
        return cls(
            max_steps=s.agent_max_steps,
            max_tokens=s.agent_max_total_tokens,
            timeout_s=s.agent_timeout_s,
            max_invalid_calls=s.agent_max_invalid_calls,
        )


@dataclass(frozen=True)
class InvestigationResult:
    status: Status
    state: State
    plan: Plan | None
    files_read: list[str]
    steps: int
    input_tokens: int
    output_tokens: int
    events: list[AgentEvent] = field(default_factory=list)
    error: str | None = None


async def run_investigation(
    llm: LLM,
    dispatcher: Dispatcher,
    issue: Issue,
    budgets: Budgets | None = None,
    clock: Callable[[], float] = time.monotonic,
    events: EventLog | None = None,
    machine: StateMachine | None = None,
) -> InvestigationResult:
    """Run the read-only investigation and return a result with a plan, or why it stopped.

    Pass `events` and `machine` to continue a larger run; otherwise fresh ones are made.
    """
    budgets = budgets or Budgets()
    events = events or EventLog()
    machine = machine or StateMachine(events)
    if machine.state is State.INIT:
        machine.transition(State.INVESTIGATE, "start")

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": issue_prompt(issue)},
    ]
    tools = tool_specs(dispatcher.allowed) + [
        function_spec(SUBMIT_PLAN, SUBMIT_PLAN_DESCRIPTION, Plan)
    ]
    counters = {"steps": 0, "in": 0, "out": 0}
    started = clock()
    invalid = 0

    def finish(status: Status, reason: str, plan: Plan | None = None, error: str | None = None):
        if status is not Status.PLAN_READY:
            machine.transition(State.ABORTED, reason)
        return InvestigationResult(
            status=status,
            state=machine.state,
            plan=plan,
            files_read=sorted(dispatcher.files_read),
            steps=counters["steps"],
            input_tokens=counters["in"],
            output_tokens=counters["out"],
            events=events.events,
            error=error,
        )

    def stop(reason: str):
        events.record("budget", state=machine.state.value, reason=reason)
        return finish(Status.BUDGET_EXHAUSTED, reason)

    def too_many_invalid():
        return finish(Status.ABORTED, "too many invalid calls in a row")

    while True:
        if counters["steps"] >= budgets.max_steps:
            return stop("step limit")
        if counters["in"] + counters["out"] >= budgets.max_tokens:
            return stop("token budget")
        if clock() - started >= budgets.timeout_s:
            return stop("timeout")

        try:
            result = await llm.complete(messages, tools)
        except LLMError as exc:
            events.record("llm_error", state=machine.state.value, error_type=type(exc).__name__)
            return finish(Status.LLM_ERROR, "llm error", error=f"{type(exc).__name__}: {exc}")
        counters["steps"] += 1
        counters["in"] += result.input_tokens
        counters["out"] += result.output_tokens
        events.record(
            "llm_call",
            state=machine.state.value,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_s=round(result.latency_s, 3),
            usage_missing=result.usage_missing,
            tool_calls=len(result.tool_calls),
        )

        if not result.tool_calls:
            messages.append({"role": "assistant", "content": result.text})
            messages.append({"role": "user", "content": NO_TOOL_NUDGE})
            events.record("no_tool_call", state=machine.state.value)
            invalid += 1
            if invalid >= budgets.max_invalid_calls:
                return too_many_invalid()
            continue

        messages.append(assistant_message(result))

        for call in result.tool_calls:
            if call.name == SUBMIT_PLAN:
                machine.transition(State.PLAN, "submit_plan")
                plan, problem = _check_plan(call.arguments, dispatcher)
                if plan is not None:
                    events.record(
                        "plan_accepted",
                        state=machine.state.value,
                        files=len(plan.cited_files),
                        steps=len(plan.steps),
                    )
                    return finish(Status.PLAN_READY, "plan accepted", plan=plan)
                machine.transition(State.INVESTIGATE, "plan rejected")
                events.record("plan_rejected", state=machine.state.value, reason=problem)
                ok, content = False, problem
            else:
                events.record(
                    "tool_call",
                    state=machine.state.value,
                    name=call.name,
                    args_bytes=len(call.arguments.encode()),
                )
                outcome = dispatcher.dispatch(call)
                events.record(
                    "tool_result",
                    state=machine.state.value,
                    name=call.name,
                    ok=outcome.ok,
                    chars=len(outcome.content),
                )
                ok, content = outcome.ok, outcome.content
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            invalid = 0 if ok else invalid + 1
            if invalid >= budgets.max_invalid_calls:
                return too_many_invalid()


def _check_plan(raw: str, dispatcher: Dispatcher) -> tuple[Plan | None, str]:
    """Return (plan, "") if valid and evidence-backed, else (None, message for the model)."""
    if len(raw.encode()) > MAX_PLAN_ARGS_BYTES:
        return None, f"Error: Plan too large (max {MAX_PLAN_ARGS_BYTES} bytes)"
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None, "Error: Arguments must be a JSON object"
    try:
        plan = Plan(**data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
            for e in exc.errors()
        )
        return None, f"Error: Invalid plan: {problems}"
    unread = []
    for cited in plan.cited_files:
        norm = dispatcher.normalize(cited.path)
        if norm is None or norm not in dispatcher.files_read:
            unread.append(cited.path[:100])
        else:
            cited.path = norm
    if unread:
        shown = ", ".join(dict.fromkeys(unread))
        return None, (
            f"Error: Plan cites files you have not read: {shown}. "
            "Read them with read_file first, or remove them from the plan."
        )
    return plan, ""
