"""In-memory event log. Events hold names, counts and sizes, never file contents."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentEvent:
    ts: float
    state: str | None
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class EventLog:
    """Append-only list of events for one run.

    `on_event` is called with each new event (for live streaming). A listener that
    raises is logged and ignored, so it can never break a run.
    """

    def __init__(self, on_event: Callable[[AgentEvent], None] | None = None) -> None:
        self.events: list[AgentEvent] = []
        self._on_event = on_event

    def record(self, kind: str, state: str | None = None, **data: Any) -> None:
        event = AgentEvent(ts=time.time(), state=state, kind=kind, data=data)
        self.events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                log.exception("event listener failed")
