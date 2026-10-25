"""Agent states and the rules for moving between them. Every move is recorded."""

from enum import StrEnum

from app.agent.events import EventLog


class State(StrEnum):
    INIT = "INIT"
    INVESTIGATE = "INVESTIGATE"
    PLAN = "PLAN"
    ABORTED = "ABORTED"  # terminal: budget, error or too many invalid calls


# Later chunks add PATCH, VALIDATE, TEST, REPAIR, REPORT and so on.
ALLOWED: dict[State, set[State]] = {
    State.INIT: {State.INVESTIGATE},
    State.INVESTIGATE: {State.PLAN, State.ABORTED},
    State.PLAN: {State.INVESTIGATE, State.ABORTED},  # back to INVESTIGATE if a plan is rejected
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
