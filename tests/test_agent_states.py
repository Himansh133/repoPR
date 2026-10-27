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


def test_full_run_path_is_legal():
    log = EventLog()
    sm = StateMachine(log)
    for target in (
        State.INVESTIGATE, State.PLAN, State.PATCH, State.VALIDATE, State.TEST,
        State.REPAIR, State.VALIDATE, State.TEST, State.REPORT,
    ):
        sm.transition(target, "step")
    assert sm.state is State.REPORT
    with pytest.raises(IllegalTransition):
        sm.transition(State.ABORTED, "too late")  # REPORT is terminal


def test_rejected_patch_can_go_back():
    sm = StateMachine(EventLog())
    for target in (State.INVESTIGATE, State.PLAN, State.PATCH, State.VALIDATE, State.PATCH):
        sm.transition(target, "step")
    assert sm.state is State.PATCH


@pytest.mark.parametrize("state", [State.PATCH, State.VALIDATE, State.TEST, State.REPAIR])
def test_can_abort_from_every_working_state(state):
    paths = {
        State.PATCH: [State.INVESTIGATE, State.PLAN, State.PATCH],
        State.VALIDATE: [State.INVESTIGATE, State.PLAN, State.PATCH, State.VALIDATE],
        State.TEST: [State.INVESTIGATE, State.PLAN, State.PATCH, State.VALIDATE, State.TEST],
        State.REPAIR: [
            State.INVESTIGATE, State.PLAN, State.PATCH, State.VALIDATE, State.TEST, State.REPAIR
        ],
    }
    sm = StateMachine(EventLog())
    for target in paths[state]:
        sm.transition(target, "step")
    sm.transition(State.ABORTED, "stop")
    assert sm.state is State.ABORTED


@pytest.mark.parametrize(
    "start,target",
    [(State.PATCH, State.TEST), (State.PATCH, State.REPORT), (State.TEST, State.PATCH)],
)
def test_skipping_validation_or_testing_is_illegal(start, target):
    sm = StateMachine(EventLog())
    path = {
        State.PATCH: [State.INVESTIGATE, State.PLAN, State.PATCH],
        State.TEST: [State.INVESTIGATE, State.PLAN, State.PATCH, State.VALIDATE, State.TEST],
    }[start]
    for step in path:
        sm.transition(step, "step")
    with pytest.raises(IllegalTransition):
        sm.transition(target, "nope")


def test_event_log_calls_the_listener_for_each_event():
    seen = []
    log = EventLog(on_event=seen.append)
    log.record("baseline", "INVESTIGATE", status="failed")
    assert seen == log.events and seen[0].kind == "baseline"


def test_a_failing_listener_never_breaks_the_run():
    def boom(event):
        raise RuntimeError("listener bug")

    log = EventLog(on_event=boom)
    log.record("x")
    assert len(log.events) == 1
