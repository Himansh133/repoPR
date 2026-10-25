"""Tests for the state machine and the event log."""

import pytest

from app.agent.events import EventLog
from app.agent.states import IllegalTransition, State, StateMachine


def test_starts_in_init():
    assert StateMachine(EventLog()).state is State.INIT


def test_legal_path_and_events_recorded():
    log = EventLog()
    sm = StateMachine(log)
    sm.transition(State.INVESTIGATE, "start")
    sm.transition(State.PLAN, "submit_plan")
    sm.transition(State.INVESTIGATE, "plan rejected")
    sm.transition(State.ABORTED, "step limit")
    kinds = [(e.kind, e.data["from"], e.data["to"]) for e in log.events]
    assert kinds == [
        ("transition", "INIT", "INVESTIGATE"),
        ("transition", "INVESTIGATE", "PLAN"),
        ("transition", "PLAN", "INVESTIGATE"),
        ("transition", "INVESTIGATE", "ABORTED"),
    ]
    assert log.events[-1].data["reason"] == "step limit"


@pytest.mark.parametrize(
    "start,target",
    [
        (State.INIT, State.PLAN),
        (State.INIT, State.ABORTED),
        (State.INVESTIGATE, State.INIT),
        (State.PLAN, State.INIT),
    ],
)
def test_illegal_transitions_raise(start, target):
    sm = StateMachine(EventLog())
    path = {
        State.INIT: [],
        State.INVESTIGATE: [State.INVESTIGATE],
        State.PLAN: [State.INVESTIGATE, State.PLAN],
    }[start]
    for step in path:
        sm.transition(step, "setup")
    with pytest.raises(IllegalTransition):
        sm.transition(target, "nope")
    assert sm.state is start


def test_aborted_is_terminal():
    sm = StateMachine(EventLog())
    sm.transition(State.INVESTIGATE, "x")
    sm.transition(State.ABORTED, "x")
    with pytest.raises(IllegalTransition):
        sm.transition(State.INVESTIGATE, "x")


def test_event_log_records_kind_and_data_with_timestamp():
    log = EventLog()
    log.record("tool_call", state="INVESTIGATE", name="read_file", ok=True)
    event = log.events[0]
    assert event.kind == "tool_call"
    assert event.data == {"name": "read_file", "ok": True}
    assert event.state == "INVESTIGATE"
    assert event.ts > 0
