"""Tests for app.db: SQLite persistence, usage and cost totals, daily limits."""

import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.events import AgentEvent
from app.agent.fix import RunStatus
from app.db import BudgetBlocked, Database
from app.patch import PatchResult
from app.sandbox import ExecStatus
from tests.fakes import execution
from tests.test_report import ISSUE
from tests.test_report import result as make_result

PRICES = (Decimal("2"), Decimal("8"))


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path, clock):
    database = Database(tmp_path / "t.db", now=clock)
    yield database
    database.close()


def llm_event(i, tokens_in, tokens_out, latency=1.5, missing=False):
    return AgentEvent(
        ts=1000.0 + i, state="INVESTIGATE", kind="llm_call",
        data={"input_tokens": tokens_in, "output_tokens": tokens_out,
              "latency_s": latency, "usage_missing": missing, "tool_calls": 1},
    )


def run_with(events=None, **kw):
    events = events if events is not None else [llm_event(0, 1000, 500), llm_event(1, 2000, 100)]
    patch = PatchResult(
        files=["shop/cart.py"], added=2, removed=0, branch="repopilot/issue-7",
        commit_sha="abc123", diff="--- a/shop/cart.py\n+++ b/shop/cart.py\n+    if not prices:\n",
    )
    base = dict(
        events=events, patches=[patch], input_tokens=3000, output_tokens=600,
        baseline=execution(ExecStatus.FAILED, ["tests/test_cart.py::test_empty"],
                           log="SECRET-TEST-LOG"),
        executions=[execution(ExecStatus.PASSED, log="SECRET-TEST-LOG-2")],
    )
    base.update(kw)
    return make_result(RunStatus.PASSED, **base)


def save(db, result=None, prices=PRICES, model="m1", report="REPORT TEXT"):
    run_id = db.start_run("octo/demo", ISSUE.number, ISSUE.title, model)
    db.finish_run(run_id, result or run_with(), report, prices)
    return run_id


# --- schema -------------------------------------------------------------------

def test_schema_is_created_and_reopening_is_safe(tmp_path):
    path = tmp_path / "x.db"
    Database(path).close()
    db = Database(path)
    db.close()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"runs", "events", "patches", "test_executions", "usage_records", "approvals"} <= tables
    assert conn.execute("pragma user_version").fetchone()[0] == 1


def test_database_file_is_private(tmp_path):
    Database(tmp_path / "x.db").close()
    mode = stat.S_IMODE((tmp_path / "x.db").stat().st_mode)
    assert mode & 0o077 == 0


def test_data_survives_reopen(tmp_path, clock):
    path = tmp_path / "x.db"
    db = Database(path, now=clock)
    run_id = save(db)
    db.close()
    again = Database(path, now=clock)
    assert again.get_run(run_id).status == "passed"
    again.close()


# --- runs ---------------------------------------------------------------------

def test_start_run_creates_a_running_row(db):
    run_id = db.start_run("octo/demo", 7, "Crash", "m1")
    run = db.get_run(run_id)
    assert (run.status, run.repo, run.issue_number, run.model) == ("running", "octo/demo", 7, "m1")
    assert run.started_at.startswith("2026-10-27T09:00")
    assert run.finished_at is None


def test_finish_run_stores_summary(db):
    run_id = save(db, report="THE REPORT")
    run = db.get_run(run_id)
    assert run.status == "passed" and run.reason is None
    assert run.branch == "repopilot/issue-7"
    assert run.workdir == "/x"
    assert (run.repairs_used, run.steps) == (0, 3)
    assert (run.input_tokens, run.output_tokens) == (3000, 600)
    assert run.report == "THE REPORT"
    assert run.finished_at is not None


def test_unknown_run_is_none(db):
    assert db.get_run(999) is None


def test_list_runs_newest_first_with_limit(db, clock):
    ids = []
    for _ in range(3):
        ids.append(save(db))
        clock.t += timedelta(minutes=1)
    assert [r.id for r in db.list_runs()] == ids[::-1]
    assert len(db.list_runs(limit=2)) == 2


def test_fail_run_marks_error(db):
    run_id = db.start_run("octo/demo", 7, "t", "m1")
    db.fail_run(run_id, "could not clone the repository")
    run = db.get_run(run_id)
    assert run.status == "error" and "clone" in run.reason


# --- child rows ---------------------------------------------------------------

def test_events_round_trip_in_order(db):
    run_id = save(db)
    events = db.get_events(run_id)
    assert [e["kind"] for e in events] == ["llm_call", "llm_call"]
    assert events[0]["data"]["input_tokens"] == 1000
    assert events[0]["state"] == "INVESTIGATE"


def test_non_json_event_data_does_not_crash(db):
    ev = AgentEvent(ts=1.0, state=None, kind="x", data={"path": Path("/a/b")})
    run_id = save(db, run_with(events=[ev]))
    assert db.get_events(run_id)[0]["data"]["path"] == "/a/b"


def test_patches_are_stored_with_their_diff(db):
    run_id = save(db)
    (patch,) = db.get_patches(run_id)
    assert patch["files"] == ["shop/cart.py"]
    assert (patch["added"], patch["removed"], patch["commit_sha"]) == (2, 0, "abc123")
    assert "+    if not prices:" in patch["diff"]


def test_test_executions_store_names_and_status_but_not_logs(db):
    run_id = save(db)
    rows = db.get_executions(run_id)
    assert [(r["kind"], r["status"]) for r in rows] == [
        ("baseline", "failed"), ("after_patch", "passed"),
    ]
    assert rows[0]["failed_tests"] == ["tests/test_cart.py::test_empty"]
    assert "log_tail" not in rows[0]
    conn = sqlite3.connect(db.path)
    dump = " ".join(str(v) for t in ("runs", "events", "patches", "test_executions",
                                      "usage_records")
                    for row in conn.execute(f"select * from {t}") for v in row)
    assert "SECRET-TEST-LOG" not in dump


# --- usage and cost -----------------------------------------------------------

def test_usage_records_and_exact_cost(db):
    run_id = save(db)
    usage = db.get_usage(run_id)
    assert [(u["input_tokens"], u["output_tokens"]) for u in usage] == [(1000, 500), (2000, 100)]
    assert [u["cost"] for u in usage] == [Decimal("0.006"), Decimal("0.0048")]
    assert usage[0]["model"] == "m1" and usage[0]["latency_s"] == 1.5
    run = db.get_run(run_id)
    assert run.cost == Decimal("0.0108") and run.cost_estimated is False


def test_missing_provider_usage_marks_cost_as_estimate(db):
    events = [llm_event(0, 1000, 500, missing=True)]
    run_id = save(db, run_with(events=events))
    assert db.get_run(run_id).cost_estimated is True
    assert db.get_usage(run_id)[0]["usage_missing"] is True


def test_unknown_prices_give_unknown_cost(db):
    run_id = save(db, prices=(None, None))
    assert db.get_run(run_id).cost is None
    assert all(u["cost"] is None for u in db.get_usage(run_id))


def test_totals_for_a_day_and_overall(db, clock):
    save(db)
    save(db, prices=(None, None))
    clock.t += timedelta(days=1)
    save(db)
    today = db.usage_totals(clock.t.date())
    assert today.runs == 1 and today.cost == Decimal("0.0108")
    assert (today.input_tokens, today.output_tokens) == (3000, 600)
    everything = db.usage_totals()
    assert everything.runs == 3
    assert everything.cost == Decimal("0.0216")
    assert everything.unknown_cost_runs == 1
    assert (everything.input_tokens, everything.output_tokens) == (9000, 1800)


# --- daily limits -------------------------------------------------------------

def check(db, cap=Decimal("5"), max_runs=5, priced=True, allow_unpriced=False):
    db.check_can_start(cap, max_runs, priced=priced, allow_unpriced=allow_unpriced)


def test_allowed_when_under_limits(db):
    save(db)
    check(db)


def test_blocks_at_the_daily_run_limit_and_counts_running_and_failed_runs(db):
    save(db)
    db.start_run("o/r", 1, "t", "m")  # still running
    failed = db.start_run("o/r", 2, "t", "m")
    db.fail_run(failed, "boom")
    check(db, max_runs=4)
    with pytest.raises(BudgetBlocked, match="run limit"):
        check(db, max_runs=3)


def test_blocks_when_daily_spend_reaches_the_cap(db):
    save(db)  # spend 0.0108
    check(db, cap=Decimal("0.02"))
    with pytest.raises(BudgetBlocked, match="spend cap"):
        check(db, cap=Decimal("0.0108"))
    with pytest.raises(BudgetBlocked, match="spend cap"):
        check(db, cap=Decimal("0.01"))


def test_limits_reset_the_next_day(db, clock):
    for _ in range(5):
        save(db)
    with pytest.raises(BudgetBlocked):
        check(db)
    clock.t += timedelta(days=1)
    check(db)


def test_no_spend_cap_means_only_the_run_limit_applies(db):
    save(db)
    check(db, cap=None)


def test_unpriced_runs_are_blocked_unless_allowed(db):
    with pytest.raises(BudgetBlocked, match="PRICE_IN_PER_M"):
        check(db, priced=False)
    check(db, priced=False, allow_unpriced=True)


# --- approvals (used by the PR step later) ------------------------------------

def test_approvals_round_trip(db):
    run_id = save(db)
    assert db.get_approvals(run_id) == []
    db.record_approval(run_id, "approved", "looks good")
    (a,) = db.get_approvals(run_id)
    assert a["decision"] == "approved" and a["note"] == "looks good"
    with pytest.raises(ValueError):
        db.record_approval(run_id, "maybe", "")
