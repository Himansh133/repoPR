"""The HTTP API: the same runs and approvals as the CLI, over FastAPI.

Every endpoint except `/health` needs `X-API-Key: <REPOPILOT_API_KEY>`. Runs execute one
at a time in a worker thread (the sandbox blocks), and their events stream live over
server-sent events. Write endpoints are rate limited; the daily run and spend caps still
apply. Start it with `repopilot serve`, which binds to 127.0.0.1 by default.
"""

import asyncio
import hmac
import json
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app import __version__
from app.agent.events import AgentEvent
from app.config import Settings
from app.db import Database, RunRecord
from app.service import (
    Conflict,
    Deps,
    NotFound,
    PreparedRun,
    RunRequest,
    ServiceError,
    execute_run,
    issue_from_fields,
    load_settings,
    open_db,
    open_draft_pr,
    plan_approval,
    prepare_run,
    reject_run,
)

MIN_KEY_CHARS = 16
STALE_AFTER = timedelta(hours=1)  # far longer than the 5-minute run timeout
KEEP_LIVE_RUNS = 5
SSE_POLL_S = 0.1
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "dist"


class ApiError(Exception):
    """The API cannot start (for example, no API key)."""


# --- request bodies --------------------------------------------------------------------------

class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(min_length=1, max_length=500, description="local git repo or GitHub URL")
    issue: int = Field(ge=1, le=1_000_000_000)
    github: str | None = Field(default=None, max_length=140)
    issue_title: str | None = Field(default=None, min_length=1, max_length=300,
                                    description="give the issue directly instead of GitHub")
    issue_body: str = Field(default="", max_length=10_000)
    allow_unpriced: bool = False


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool
    closes_issue: bool = False
    note: str = Field(default="", max_length=500)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=500)


# --- rate limits and the run worker ----------------------------------------------------------

class RateLimiter:
    """At most `limit` hits per `window_s` seconds. `hit()` returns seconds to wait, or 0."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def hit(self) -> float:
        with self._lock:
            now = self.clock()
            while self._hits and now - self._hits[0] >= self.window_s:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return self.window_s - (now - self._hits[0])
            self._hits.append(now)
            return 0


class RunManager:
    """Runs one agent run at a time in a worker thread and keeps its events for streaming."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._current: int | None = None
        self._events: dict[int, list[dict[str, Any]]] = {}
        self._done: set[int] = set()

    def reserve(self) -> None:
        with self._lock:
            if self._busy:
                raise Conflict("A run is already in progress; try again when it finishes")
            self._busy = True

    def release(self) -> None:
        with self._lock:
            self._busy = False
            self._current = None

    def is_running(self, run_id: int) -> bool:
        with self._lock:
            return self._current == run_id

    def start(self, prep: PreparedRun, settings: Settings, deps: Deps) -> None:
        """Run `prep` in a new thread. The slot must already be reserved."""
        with self._lock:
            self._current = prep.run_id
            self._events[prep.run_id] = []
            for old in [r for r in self._events if r in self._done][:-KEEP_LIVE_RUNS]:
                self._events.pop(old, None)
                self._done.discard(old)
        threading.Thread(target=self._work, args=(prep, settings, deps), daemon=True,
                         name=f"repopilot-run-{prep.run_id}").start()

    def _work(self, prep: PreparedRun, settings: Settings, deps: Deps) -> None:
        def listen(event: AgentEvent) -> None:
            with self._lock:
                self._events[prep.run_id].append(_event_json(event))

        db = open_db(deps)
        try:
            execute_run(prep, settings, deps, db, on_event=listen)
        except Exception:  # already recorded on the run by execute_run
            pass
        finally:
            db.close()
            with self._lock:
                self._done.add(prep.run_id)
            self.release()

    def snapshot(self, run_id: int, since: int) -> tuple[list[dict[str, Any]], bool] | None:
        """(new events, finished) for a run this process started, else None."""
        with self._lock:
            if run_id not in self._events:
                return None
            finished = run_id in self._done
            return self._events[run_id][since:], finished


def _event_json(event: AgentEvent | dict) -> dict[str, Any]:
    if isinstance(event, AgentEvent):
        event = {"ts": event.ts, "state": event.state, "kind": event.kind, "data": event.data}
    return json.loads(json.dumps(event, default=str))


def _sse(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def _money(value) -> str | None:
    return None if value is None else format(value, "f")


def _run_json(run: RunRecord, full: bool = True) -> dict[str, Any]:
    data = {
        "id": run.id, "started_at": run.started_at, "finished_at": run.finished_at,
        "repo": run.repo, "issue_number": run.issue_number, "issue_title": run.issue_title,
        "model": run.model, "status": run.status, "reason": run.reason, "branch": run.branch,
        "repairs_used": run.repairs_used, "steps": run.steps,
        "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "cost": _money(run.cost), "cost_estimated": run.cost_estimated, "pr_url": run.pr_url,
    }
    if full:
        data["report"] = run.report
    return data


# --- the app ---------------------------------------------------------------------------------

def create_app(
    deps: Deps | None = None,
    *,
    run_rate: tuple[int, float] = (5, 60),
    decide_rate: tuple[int, float] = (10, 60),
    dashboard_dir: Path = DASHBOARD_DIR,
) -> FastAPI:
    """Build the app. Raises ApiError if REPOPILOT_API_KEY is missing or shorter than 16."""
    deps = deps or Deps()
    key = deps.paths().repopilot_api_key
    if key is None:
        raise ApiError("Set REPOPILOT_API_KEY to start the API")
    if len(key.get_secret_value()) < MIN_KEY_CHARS:
        raise ApiError(f"REPOPILOT_API_KEY must be at least {MIN_KEY_CHARS} characters")
    key_bytes = key.get_secret_value().encode()

    db = open_db(deps)
    try:
        db.fail_stale_runs(datetime.now(UTC) - STALE_AFTER)
    finally:
        db.close()

    manager = RunManager()
    run_limiter = RateLimiter(*run_rate)
    decide_limiter = RateLimiter(*decide_rate)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not x_api_key or not hmac.compare_digest(x_api_key.encode(), key_bytes):
            raise HTTPException(401, "Missing or wrong X-API-Key")

    def limited(limiter: RateLimiter) -> None:
        wait = limiter.hit()
        if wait:
            raise HTTPException(429, "Too many requests", {"Retry-After": str(max(1, int(wait)))})

    def with_db(fn: Callable[[Database], Any]) -> Any:
        db = open_db(deps)
        try:
            return fn(db)
        finally:
            db.close()

    def get_run(db: Database, run_id: int) -> RunRecord:
        run = db.get_run(run_id)
        if run is None:
            raise NotFound(f"No run with id {run_id}")
        return run

    app = FastAPI(title="RepoPilot", version=__version__)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/health")
    def health() -> dict[str, Any]:
        try:
            deps.settings()
            configured = True
        except Exception:
            configured = False
        return {"status": "ok", "version": __version__, "gmi_configured": configured,
                "docker": shutil.which("docker") is not None}

    api = APIRouter(dependencies=[Depends(require_key)])

    @api.post("/runs", status_code=202)
    def create_run(body: RunIn) -> dict[str, Any]:
        limited(run_limiter)
        manager.reserve()
        try:
            settings = load_settings(deps)
            issue = (issue_from_fields(body.issue, body.issue_title, body.issue_body)
                     if body.issue_title else None)
            request = RunRequest(body.repo, body.issue, body.github, issue, body.allow_unpriced)
            prep = with_db(lambda db: prepare_run(request, settings, deps, db))
        except BaseException:
            manager.release()
            raise
        manager.start(prep, settings, deps)
        return {"id": prep.run_id, "status": "running", "events": f"/runs/{prep.run_id}/events"}

    @api.get("/runs")
    def list_runs(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        return {"runs": [_run_json(r, full=False) for r in with_db(lambda db: db.list_runs(limit))]}

    @api.get("/runs/{run_id}")
    def read_run(run_id: int) -> dict[str, Any]:
        def go(db):
            run = get_run(db, run_id)
            approvals = db.get_approvals(run.id)
            return {**_run_json(run), "decision": approvals[-1] if approvals else None}
        return with_db(go)

    @api.get("/runs/{run_id}/diff")
    def read_diff(run_id: int) -> dict[str, Any]:
        def go(db):
            get_run(db, run_id)
            return {"patches": db.get_patches(run_id)}
        return with_db(go)

    @api.get("/runs/{run_id}/tests")
    def read_tests(run_id: int) -> dict[str, Any]:
        def go(db):
            get_run(db, run_id)
            return {"executions": db.get_executions(run_id)}
        return with_db(go)

    @api.get("/runs/{run_id}/cost")
    def read_cost(run_id: int) -> dict[str, Any]:
        def go(db):
            run = get_run(db, run_id)
            calls = [{**c, "cost": _money(c["cost"])} for c in db.get_usage(run_id)]
            return {"total": _money(run.cost), "estimated": run.cost_estimated,
                    "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
                    "calls": calls}
        return with_db(go)

    @api.get("/runs/{run_id}/events")
    async def stream_events(run_id: int) -> StreamingResponse:
        await asyncio.to_thread(with_db, lambda db: get_run(db, run_id))

        async def gen():
            sent = 0
            while (snap := manager.snapshot(run_id, sent)) is not None:
                events, finished = snap
                for event in events:
                    yield _sse(event["kind"], event)
                sent += len(events)
                if finished:
                    break
                await asyncio.sleep(SSE_POLL_S)
            else:  # not started by this process: replay what is stored
                stored = await asyncio.to_thread(with_db, lambda db: db.get_events(run_id))
                for event in stored:
                    yield _sse(event["kind"], _event_json(event))
            run = await asyncio.to_thread(with_db, lambda db: get_run(db, run_id))
            yield _sse("end", {"status": run.status})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @api.post("/runs/{run_id}/approve")
    def approve(run_id: int, body: ApproveIn) -> dict[str, Any]:
        limited(decide_limiter)
        if not body.confirm:
            raise ServiceError("Set confirm to true to create a draft PR. Nothing was sent.")
        if manager.is_running(run_id):
            raise Conflict(f"Run #{run_id} is still running")

        def go(db):
            plan = plan_approval(db, deps, run_id, body.closes_issue)
            return {"id": run_id, "pr_url": open_draft_pr(db, deps, plan, body.note)}
        return with_db(go)

    @api.post("/runs/{run_id}/reject")
    def reject(run_id: int, body: RejectIn | None = None) -> dict[str, Any]:
        limited(decide_limiter)
        if manager.is_running(run_id):
            raise Conflict(f"Run #{run_id} is still running")
        note = body.note if body else ""
        run = with_db(lambda db: reject_run(db, run_id, note))
        return {"id": run.id, "decision": "rejected"}

    app.include_router(api)
    # The dashboard is static files; it needs no key, but every data call it makes does.
    if (dashboard_dir / "index.html").is_file():
        app.mount("/ui", StaticFiles(directory=dashboard_dir, html=True), name="ui")
    else:
        @app.get("/ui/", response_class=PlainTextResponse)
        def dashboard_missing() -> str:
            return "Dashboard not built: cd dashboard && npm ci && npm run build"
    return app
