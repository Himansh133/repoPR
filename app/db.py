"""SQLite persistence: runs, events, patches, test results, usage, approvals.

Uses the standard-library `sqlite3`. Costs are stored as text (exact `Decimal`), never
float. Test logs and file contents are not stored; patch diffs (max 20 KB, model-written)
are, because the PR step needs them. The file is private (mode 0600).
"""

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.agent.events import AgentEvent
from app.agent.fix import RunResult
from app.llm import calculate_cost

SCHEMA_VERSION = 2
_SCHEMA_V1 = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    branch TEXT,
    workdir TEXT,
    repairs_used INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost TEXT,
    cost_estimated INTEGER NOT NULL DEFAULT 0,
    report TEXT
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    state TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    files TEXT NOT NULL,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    diff TEXT NOT NULL
);
CREATE TABLE test_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    duration_s REAL NOT NULL,
    failed_tests TEXT NOT NULL,
    truncated INTEGER NOT NULL,
    reason TEXT
);
CREATE TABLE usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    usage_missing INTEGER NOT NULL,
    latency_s REAL NOT NULL,
    cost TEXT
);
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE INDEX idx_runs_started ON runs(started_at);
"""
# Each step takes the database from version N-1 to N. Existing rows are kept.
_MIGRATIONS = {
    2: "ALTER TABLE runs ADD COLUMN pr_url TEXT;",
}


@dataclass(frozen=True)
class UsageSummary:
    """Per-call costs for one run. `total` is None if any call's cost is unknown."""

    calls: list[tuple[dict, Decimal | None]]
    total: Decimal | None
    estimated: bool  # the provider omitted token usage for at least one call


def summarize_usage(
    events: list[AgentEvent], prices: tuple[Decimal | None, Decimal | None]
) -> UsageSummary:
    """Work out the cost of each `llm_call` event and the run total."""
    calls = []
    for event in events:
        if event.kind != "llm_call":
            continue
        d = event.data
        calls.append((d, calculate_cost(d["input_tokens"], d["output_tokens"], *prices)))
    known = [c for _, c in calls if c is not None]
    total = sum(known, Decimal(0)) if calls and len(known) == len(calls) else None
    return UsageSummary(calls, total, any(d.get("usage_missing") for d, _ in calls))


class BudgetBlocked(Exception):
    """A new run may not start (daily limit, spend cap or missing prices)."""


@dataclass(frozen=True)
class RunRecord:
    id: int
    started_at: str
    finished_at: str | None
    repo: str
    issue_number: int
    issue_title: str
    model: str
    status: str
    reason: str | None
    branch: str | None
    workdir: str | None
    repairs_used: int
    steps: int
    input_tokens: int
    output_tokens: int
    cost: Decimal | None
    cost_estimated: bool
    report: str | None
    pr_url: str | None = None


@dataclass(frozen=True)
class UsageTotals:
    runs: int
    input_tokens: int
    output_tokens: int
    cost: Decimal
    unknown_cost_runs: int


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


class Database:
    """A small wrapper over one SQLite file."""

    def __init__(
        self,
        path: str | Path,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        os.chmod(self.path, 0o600)
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database was created by a newer RepoPilot version")
        if version == 0:
            with self._conn:
                self._conn.executescript(_SCHEMA_V1)
                self._conn.execute("PRAGMA user_version = 1")
            version = 1
        for step in range(version + 1, SCHEMA_VERSION + 1):  # one transaction per step
            self._conn.executescript(
                f"BEGIN; {_MIGRATIONS[step]} PRAGMA user_version = {step}; COMMIT;"
            )

    def close(self) -> None:
        self._conn.close()

    # --- runs ---------------------------------------------------------------------------

    def start_run(self, repo: str, issue_number: int, issue_title: str, model: str) -> int:
        """Record that a run began. It counts toward the daily limits straight away."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, repo, issue_number, issue_title, model, status)"
                " VALUES (?, ?, ?, ?, ?, 'running')",
                (self._now().isoformat(timespec="seconds"), repo, issue_number,
                 issue_title[:300], model),
            )
        return int(cur.lastrowid)

    def fail_stale_runs(self, started_before: datetime) -> int:
        """Mark runs still 'running' that began before `started_before` as errors.

        Used at API startup: those runs belonged to a process that stopped.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status='error', reason=?, finished_at=?"
                " WHERE status='running' AND started_at < ?",
                ("interrupted: the process running it stopped",
                 self._now().isoformat(timespec="seconds"),
                 started_before.isoformat(timespec="seconds")),
            )
        return cur.rowcount

    def fail_run(self, run_id: int, reason: str) -> None:
        """Mark a run that crashed before producing a result."""
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status='error', reason=?, finished_at=? WHERE id=?",
                (reason[:500], self._now().isoformat(timespec="seconds"), run_id),
            )

    def finish_run(
        self,
        run_id: int,
        result: RunResult,
        report: str,
        prices: tuple[Decimal | None, Decimal | None] = (None, None),
    ) -> RunRecord:
        """Save a finished run with its events, patches, test results and usage."""
        model = self._conn.execute("SELECT model FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        summary = summarize_usage(result.events, prices)
        usage, total, estimated = summary.calls, summary.total, summary.estimated

        with self._conn:
            c = self._conn
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, reason=?, branch=?, workdir=?,"
                " repairs_used=?, steps=?, input_tokens=?, output_tokens=?, cost=?,"
                " cost_estimated=?, report=? WHERE id=?",
                (self._now().isoformat(timespec="seconds"), result.status.value, result.reason,
                 result.branch, str(result.workdir), result.repairs_used, result.steps,
                 result.input_tokens, result.output_tokens, _money(total), int(estimated),
                 report, run_id),
            )
            c.executemany(
                "INSERT INTO events (run_id, seq, ts, state, kind, data) VALUES (?,?,?,?,?,?)",
                [(run_id, i, e.ts, e.state, e.kind, json.dumps(e.data, default=str))
                 for i, e in enumerate(result.events)],
            )
            c.executemany(
                "INSERT INTO patches (run_id, seq, commit_sha, files, added, removed, diff)"
                " VALUES (?,?,?,?,?,?,?)",
                [(run_id, i, p.commit_sha, json.dumps(p.files), p.added, p.removed, p.diff)
                 for i, p in enumerate(result.patches)],
            )
            executions = ([("baseline", result.baseline)] if result.baseline else []) + [
                ("after_patch", e) for e in result.executions
            ]
            c.executemany(
                "INSERT INTO test_executions (run_id, seq, kind, status, exit_code, duration_s,"
                " failed_tests, truncated, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                [(run_id, i, kind, e.status.value, e.exit_code, e.duration_s,
                  json.dumps(e.failed_tests), int(e.truncated), e.reason)
                 for i, (kind, e) in enumerate(executions)],
            )
            c.executemany(
                "INSERT INTO usage_records (run_id, seq, model, input_tokens, output_tokens,"
                " usage_missing, latency_s, cost) VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, i, model, d["input_tokens"], d["output_tokens"],
                  int(bool(d.get("usage_missing"))), d.get("latency_s", 0.0), _money(cost))
                 for i, (d, cost) in enumerate(usage)],
            )
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> RunRecord | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._record(row) if row else None

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._record(r) for r in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        d = dict(row)
        d["cost"] = _decimal(d["cost"])
        d["cost_estimated"] = bool(d["cost_estimated"])
        return RunRecord(**d)

    # --- child rows -----------------------------------------------------------------------

    def get_events(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, state, kind, data FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [{"ts": r["ts"], "state": r["state"], "kind": r["kind"],
                 "data": json.loads(r["data"])} for r in rows]

    def get_patches(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT commit_sha, files, added, removed, diff FROM patches WHERE run_id=?"
            " ORDER BY seq", (run_id,)
        ).fetchall()
        return [{**dict(r), "files": json.loads(r["files"])} for r in rows]

    def get_executions(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT kind, status, exit_code, duration_s, failed_tests, truncated, reason"
            " FROM test_executions WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [{**dict(r), "failed_tests": json.loads(r["failed_tests"]),
                 "truncated": bool(r["truncated"])} for r in rows]

    def get_usage(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT model, input_tokens, output_tokens, usage_missing, latency_s, cost"
            " FROM usage_records WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [{**dict(r), "usage_missing": bool(r["usage_missing"]),
                 "cost": _decimal(r["cost"])} for r in rows]

    # --- totals and limits ------------------------------------------------------------------

    def usage_totals(self, day: date | None = None) -> UsageTotals:
        """Totals over finished runs, for one UTC day or for all time."""
        sql = "SELECT * FROM runs WHERE status != 'running'"
        args: tuple = ()
        if day is not None:
            sql += " AND substr(started_at, 1, 10) = ?"
            args = (day.isoformat(),)
        rows = self._conn.execute(sql, args).fetchall()
        cost = sum((Decimal(r["cost"]) for r in rows if r["cost"] is not None), Decimal(0))
        unknown = sum(1 for r in rows if r["cost"] is None and r["status"] != "error")
        return UsageTotals(
            runs=len(rows),
            input_tokens=sum(r["input_tokens"] for r in rows),
            output_tokens=sum(r["output_tokens"] for r in rows),
            cost=cost,
            unknown_cost_runs=unknown,
        )

    def check_can_start(
        self,
        spend_cap: Decimal | None,
        max_runs: int,
        priced: bool = True,
        allow_unpriced: bool = False,
    ) -> None:
        """Raise BudgetBlocked if a new run is not allowed today (UTC)."""
        if not priced and not allow_unpriced:
            raise BudgetBlocked(
                "Prices are not set (PRICE_IN_PER_M, PRICE_OUT_PER_M), so spend cannot be "
                "measured. Set them, or pass --allow-unpriced."
            )
        today = self._now().date()
        count = self._conn.execute(
            "SELECT COUNT(*) FROM runs WHERE substr(started_at, 1, 10) = ?", (today.isoformat(),)
        ).fetchone()[0]
        if count >= max_runs:
            raise BudgetBlocked(f"Daily run limit ({max_runs}) reached")
        if spend_cap is not None:
            spent = self.usage_totals(today).cost
            if spent >= spend_cap:
                raise BudgetBlocked(
                    f"Daily spend cap ({spend_cap}) reached: {spent} spent today"
                )

    # --- approvals --------------------------------------------------------------------------

    def set_pr_url(self, run_id: int, url: str) -> None:
        """Remember the draft PR opened for a run."""
        with self._conn:
            self._conn.execute("UPDATE runs SET pr_url=? WHERE id=?", (url[:500], run_id))

    def record_approval(self, run_id: int, decision: str, note: str = "") -> None:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        with self._conn:
            self._conn.execute(
                "INSERT INTO approvals (run_id, decision, decided_at, note) VALUES (?,?,?,?)",
                (run_id, decision, self._now().isoformat(timespec="seconds"), note[:500]),
            )

    def get_approvals(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT decision, decided_at, note FROM approvals WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
