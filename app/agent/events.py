"""In-memory event log. Events hold names, counts and sizes, never file contents."""

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    ts: float
    state: str | None
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class EventLog:
    """Append-only list of events for one run."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def record(self, kind: str, state: str | None = None, **data: Any) -> None:
        self.events.append(AgentEvent(ts=time.time(), state=state, kind=kind, data=data))
