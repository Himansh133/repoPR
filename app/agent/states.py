"""Agent states and the rules for moving between them. Every move is recorded."""

from enum import StrEnum

from app.agent.events import EventLog


class State(StrEnum):
    INIT = "INIT"
    INVESTIGATE = "INVESTIGATE"
    PLAN = "PLAN"
    PATCH = "PATCH"
    VALIDATE = "VALIDATE"
    TEST = "TEST"
    REPAIR = "REPAIR"
    REPORT = "REPORT"  # terminal: the run finished and has an honest final status
    ABORTED = "ABORTED"  # terminal: budget, error, sandbox violation or too many invalid calls


ALLOWED: dict[State, set[State]] = {
    State.INIT: {State.INVESTIGATE},
    State.INVESTIGATE: {State.PLAN, State.ABORTED},
    State.PLAN: {State.INVESTIGATE, State.PATCH, State.ABORTED},  # back if a plan is rejected
    State.PATCH: {State.VALIDATE, State.ABORTED},
    State.VALIDATE: {State.PATCH, State.REPAIR, State.TEST, State.ABORTED},  # back if rejected
    State.TEST: {State.REPAIR, State.REPORT, State.ABORTED},
    State.REPAIR: {State.VALIDATE, State.ABORTED},
    State.REPORT: set(),
    State.ABORTED: set(),
}


class IllegalTransition(Exception):
    """The requested state change is not in the transition table."""


class StateMachine:
    def __init__(self, events: EventLog) -> None:
        self.state = State.INIT
        self._events = events

    def transition(self, target: State, reason: str) -> None:
        """Move to `target` or raise IllegalTransition. Records a `transition` event."""
        if target not in ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state} -> {target} is not allowed")
        self._events.record(
            "transition",
            state=target.value,
            reason=reason,
            **{"from": self.state.value, "to": target.value},
        )
        self.state = target
