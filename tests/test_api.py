"""API tests: FastAPI TestClient with the fake LLM, fake sandbox and fake PR writer.

Runs execute for real (real git, real SQLite) in the API's worker thread.
"""

import json
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import ApiError, create_app
from app.config import Settings
from app.db import Database
from app.service import Deps
from tests.fakes import FakeLLM, FakeSandbox
from tests.test_fix_run import CART, FAILING, ISSUE, PASSING, RIGHT, investigate_script, propose

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
KEY = "test-api-key-0123456789"
AUTH = {"X-API-Key": KEY}
SECRETS = ("sk-very-secret-key", "ghp_very_secret_token", "github_pat_very_secret_write", KEY)
PR_URL = "https://github.com/octo/demo/pull/9"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GMI_API_KEY", SECRETS[0])
    monkeypatch.setenv("GMI_MODEL", "test-model")
    monkeypatch.setenv("GITHUB_TOKEN", SECRETS[1])
    monkeypatch.setenv("GITHUB_WRITE_TOKEN", SECRETS[2])
    monkeypatch.setenv("REPOPILOT_API_KEY", KEY)
    monkeypatch.setenv("PRICE_IN_PER_M", "2")
    monkeypatch.setenv("PRICE_OUT_PER_M", "8")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setattr("app.tools.search.shutil.which", lambda name: None)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"],
                 ["remote", "add", "origin", "https://github.com/octo/demo.git"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=root, check=True, capture_output=True)
    return root


class FakeWriter:
    def __init__(self):
        self.calls = []

    async def create_draft_pr(self, owner, repo, branch, base_sha, files, title, body):
        self.calls.append((owner, repo, branch))
        return PR_URL


def make_deps(sandbox=None, llm=None, writer=None):
    deps = Deps(settings=lambda: Settings(_env_file=None))
    deps.llm = lambda s: llm or FakeLLM([*investigate_script(), propose(CART, RIGHT)])
    deps.sandbox = lambda s: sandbox or FakeSandbox(FAILING, PASSING)
    writer = writer or FakeWriter()
    deps.writer = lambda token: writer
    return deps


def client_for(deps=None, **kw):
    return TestClient(create_app(deps or make_deps(), **kw))


def run_body(source, **over):
    body = {"repo": str(source), "issue": 7, "issue_title": ISSUE.title, "issue_body": ISSUE.body}
    body.update(over)
    return body


def wait_done(client, run_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}", headers=AUTH).json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def start(client, source, **over):
    resp = client.post("/runs", json=run_body(source, **over), headers=AUTH)
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


def no_secrets(*texts):
    joined = "".join(texts)
    return not any(s in joined for s in SECRETS)


class Gate:
    """Holds the run at sandbox call `at` (default: the one after the patch) until released."""

    def __init__(self, at=2):
        self.at = at
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, n):
        if n == self.at:
            self.reached.set()
            assert self.release.wait(15)


# --- startup, health, auth ------------------------------------------------------------------

def test_api_refuses_to_start_without_a_strong_key(monkeypatch):
    monkeypatch.delenv("REPOPILOT_API_KEY")
    with pytest.raises(ApiError, match="REPOPILOT_API_KEY"):
        create_app(make_deps())
    monkeypatch.setenv("REPOPILOT_API_KEY", "short")
    with pytest.raises(ApiError, match="16"):
        create_app(make_deps())


def test_health_needs_no_key_and_shows_no_secrets():
    resp = client_for().get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok" and data["gmi_configured"] is True and "version" in data
    assert no_secrets(resp.text)


@pytest.mark.parametrize("method,path", [
    ("get", "/runs"), ("get", "/runs/1"), ("get", "/runs/1/events"), ("get", "/runs/1/diff"),
    ("get", "/runs/1/tests"), ("get", "/runs/1/cost"), ("post", "/runs"),
    ("post", "/runs/1/approve"), ("post", "/runs/1/reject"),
])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key-wrong-key"}, {"X-API-Key": ""}])
def test_every_endpoint_but_health_needs_the_key(method, path, headers):
    resp = getattr(client_for(), method)(path, headers=headers)
    assert resp.status_code == 401 and no_secrets(resp.text)


# --- runs -------------------------------------------------------------------------------------

def test_a_run_starts_in_the_background_and_its_results_can_be_read(source):
    client = client_for()
    run_id = start(client, source)
    run = wait_done(client, run_id)
    assert run["status"] == "passed" and run["repo"] == "octo/demo"
    assert "Status: PASSED" in run["report"] and "workdir" not in run
    diff = client.get(f"/runs/{run_id}/diff", headers=AUTH).json()
    assert diff["patches"][0]["files"] == ["shop/cart.py"]
    assert "+    if not prices:" in diff["patches"][0]["diff"]
    tests = client.get(f"/runs/{run_id}/tests", headers=AUTH).json()
    assert [(e["kind"], e["status"]) for e in tests["executions"]] == [
        ("baseline", "failed"), ("after_patch", "passed")]
    cost = client.get(f"/runs/{run_id}/cost", headers=AUTH).json()
    assert cost["total"] == "0.0018" and cost["estimated"] is False
    assert cost["calls"] and cost["calls"][0]["cost"] is not None
    listed = client.get("/runs", headers=AUTH).json()["runs"]
    assert [r["id"] for r in listed] == [run_id] and "report" not in listed[0]


@pytest.mark.parametrize("body", [
    {"repo": "x", "issue": 0},
    {"repo": "", "issue": 1},
    {"issue": 1},
    {"repo": "x", "issue": "seven"},
    {"repo": "x", "issue": 1, "unexpected": True},
    {"repo": "x", "issue": 1, "issue_body": "y" * 10_001},
])
def test_bad_run_requests_are_422(body):
    assert client_for().post("/runs", json=body, headers=AUTH).status_code == 422


def test_bad_repo_is_400_and_frees_the_run_slot(source, tmp_path):
    client = client_for()
    resp = client.post("/runs", json=run_body(tmp_path / "missing"), headers=AUTH)
    assert resp.status_code == 400 and "not found" in resp.json()["detail"]
    resp = client.post("/runs", json=run_body("https://gitlab.com/o/r"), headers=AUTH)
    assert resp.status_code == 400
    wait_done(client, start(client, source))


def test_only_one_run_at_a_time(source):
    gate = Gate()
    client = client_for(make_deps(sandbox=FakeSandbox(FAILING, PASSING, on_call=gate)))
    run_id = start(client, source)
    assert gate.reached.wait(15)
    resp = client.post("/runs", json=run_body(source), headers=AUTH)
    assert resp.status_code == 409 and "in progress" in resp.json()["detail"]
    gate.release.set()
    assert wait_done(client, run_id)["status"] == "passed"


def test_daily_run_limit_is_429(source, monkeypatch):
    monkeypatch.setenv("MAX_RUNS_PER_DAY", "1")
    client = client_for()
    wait_done(client, start(client, source))
    resp = client.post("/runs", json=run_body(source), headers=AUTH)
    assert resp.status_code == 429 and "Daily run limit" in resp.json()["detail"]


def test_write_requests_are_rate_limited(tmp_path):
    client = client_for(run_rate=(2, 60))
    for _ in range(2):
        resp = client.post("/runs", json=run_body(tmp_path / "missing"), headers=AUTH)
        assert resp.status_code == 400
    resp = client.post("/runs", json=run_body(tmp_path / "missing"), headers=AUTH)
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) >= 1


def test_a_crashing_run_is_recorded_and_frees_the_slot(source):
    client = client_for(make_deps(llm=FakeLLM([RuntimeError("kaboom")])))
    run = wait_done(client, start(client, source))
    assert run["status"] == "error" and "RuntimeError" in run["reason"]
    client2 = client_for()
    assert client2.post("/runs", json=run_body(source), headers=AUTH).status_code == 202


@pytest.mark.parametrize("method,path", [
    ("get", "/runs/42"), ("get", "/runs/42/events"), ("get", "/runs/42/diff"),
    ("get", "/runs/42/tests"), ("get", "/runs/42/cost"), ("post", "/runs/42/reject"),
])
def test_unknown_run_is_404(method, path):
    assert getattr(client_for(), method)(path, headers=AUTH).status_code == 404


def test_responses_never_contain_secrets(source):
    client = client_for()
    run_id = start(client, source)
    wait_done(client, run_id)
    texts = [client.get(p, headers=AUTH).text for p in (
        "/health", "/runs", f"/runs/{run_id}", f"/runs/{run_id}/diff",
        f"/runs/{run_id}/tests", f"/runs/{run_id}/cost", f"/runs/{run_id}/events")]
    assert no_secrets(*texts)


# --- events (SSE) -------------------------------------------------------------------------------

def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_events_of_a_finished_run_are_replayed_then_the_stream_ends(source):
    client = client_for()
    run_id = start(client, source)
    wait_done(client, run_id)
    resp = client.get(f"/runs/{run_id}/events", headers=AUTH)
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert events[0][0] == "transition"
    assert events[-1] == ("end", {"status": "passed"})
    assert any(kind == "tests_run" for kind, _ in events)


def test_events_stream_live_while_the_run_is_going(source):
    gate = Gate()
    client = client_for(make_deps(sandbox=FakeSandbox(FAILING, PASSING, on_call=gate)))
    run_id = start(client, source)
    assert gate.reached.wait(15)
    seen_while_running = []
    with client.stream("GET", f"/runs/{run_id}/events", headers=AUTH) as resp:
        lines = resp.iter_lines()
        for line in lines:
            if line.startswith("event: "):
                seen_while_running.append(line[7:])
                if "patch_applied" in seen_while_running or len(seen_while_running) >= 5:
                    break
        assert client.get(f"/runs/{run_id}", headers=AUTH).json()["status"] == "running"
        gate.release.set()
        rest = [line[7:] for line in lines if line.startswith("event: ")]
    assert seen_while_running and rest[-1] == "end"


# --- approve / reject --------------------------------------------------------------------------

def test_approve_needs_confirm_true(source):
    writer = FakeWriter()
    client = client_for(make_deps(writer=writer))
    run_id = start(client, source)
    wait_done(client, run_id)
    for body in ({}, {"confirm": False}):
        resp = client.post(f"/runs/{run_id}/approve", json=body, headers=AUTH)
        assert resp.status_code in (400, 422) and writer.calls == []
    resp = client.post(f"/runs/{run_id}/approve", json={"confirm": True}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["pr_url"] == PR_URL
    assert writer.calls == [("octo", "demo", f"repopilot/issue-7-run-{run_id}")]
    assert client.get(f"/runs/{run_id}", headers=AUTH).json()["pr_url"] == PR_URL
    again = client.post(f"/runs/{run_id}/approve", json={"confirm": True}, headers=AUTH)
    assert again.status_code == 409 and len(writer.calls) == 1


def test_reject_blocks_a_later_approve(source):
    writer = FakeWriter()
    client = client_for(make_deps(writer=writer))
    run_id = start(client, source)
    wait_done(client, run_id)
    resp = client.post(f"/runs/{run_id}/reject", json={"note": "no"}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["decision"] == "rejected"
    resp = client.post(f"/runs/{run_id}/approve", json={"confirm": True}, headers=AUTH)
    assert resp.status_code == 409 and writer.calls == []


def test_run_shows_its_latest_decision(source):
    client = client_for(make_deps(writer=FakeWriter()))
    run_id = start(client, source)
    wait_done(client, run_id)
    assert client.get(f"/runs/{run_id}", headers=AUTH).json()["decision"] is None
    client.post(f"/runs/{run_id}/reject", json={"note": "too broad"}, headers=AUTH)
    decision = client.get(f"/runs/{run_id}", headers=AUTH).json()["decision"]
    assert decision["decision"] == "rejected" and decision["note"] == "too broad"
    assert decision["decided_at"]
    listed = client.get("/runs", headers=AUTH).json()["runs"][0]
    assert "decision" not in listed


def test_a_running_run_cannot_be_approved(source):
    gate = Gate()
    writer = FakeWriter()
    client = client_for(make_deps(sandbox=FakeSandbox(FAILING, PASSING, on_call=gate),
                                  writer=writer))
    run_id = start(client, source)
    assert gate.reached.wait(15)
    resp = client.post(f"/runs/{run_id}/approve", json={"confirm": True}, headers=AUTH)
    assert resp.status_code == 409 and writer.calls == []
    gate.release.set()
    wait_done(client, run_id)


# --- cancel -------------------------------------------------------------------------------------

def test_cancel_stops_a_running_run_at_its_next_step(source):
    gate = Gate(at=1)  # hold the baseline test run
    client = client_for(make_deps(sandbox=FakeSandbox(FAILING, PASSING, on_call=gate)))
    run_id = start(client, source)
    assert gate.reached.wait(15)
    resp = client.post(f"/runs/{run_id}/cancel", headers=AUTH)
    assert resp.status_code == 202 and resp.json() == {"id": run_id, "cancelling": True}
    gate.release.set()
    run = wait_done(client, run_id)
    assert (run["status"], run["reason"]) == ("aborted", "cancelled")


def test_cancel_refuses_runs_that_are_not_running(source):
    client = client_for(make_deps())
    run_id = start(client, source)
    wait_done(client, run_id)
    resp = client.post(f"/runs/{run_id}/cancel", headers=AUTH)
    assert resp.status_code == 409 and "not running" in resp.json()["detail"]
    assert client.post("/runs/999/cancel", headers=AUTH).status_code == 404
    assert client.post(f"/runs/{run_id}/cancel").status_code == 401


# --- restarts ------------------------------------------------------------------------------------

def test_runs_left_running_by_a_dead_process_are_marked_on_startup(tmp_path):
    old = datetime.now(UTC) - timedelta(hours=2)
    db = Database(tmp_path / "api.db", now=lambda: old)
    stale = db.start_run("octo/demo", 7, "t", "m")
    db.close()
    db = Database(tmp_path / "api.db")
    fresh = db.start_run("octo/demo", 8, "t", "m")  # may belong to a CLI run still going
    db.close()
    client = client_for()
    assert client.get(f"/runs/{stale}", headers=AUTH).json()["status"] == "error"
    assert client.get(f"/runs/{fresh}", headers=AUTH).json()["status"] == "running"


# --- dashboard serving -------------------------------------------------------------------------

def test_built_dashboard_is_served_at_ui_without_a_key(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>RepoPilot</title>")
    client = TestClient(create_app(make_deps(), dashboard_dir=dist))
    resp = client.get("/ui/")
    assert resp.status_code == 200 and "<title>RepoPilot</title>" in resp.text
    assert client.get("/runs").status_code == 401  # API routes unchanged
    assert client.get("/runs", headers=AUTH).status_code == 200


def test_missing_dashboard_build_says_how_to_build(tmp_path):
    client = TestClient(create_app(make_deps(), dashboard_dir=tmp_path / "nothing"))
    resp = client.get("/ui/")
    assert resp.status_code == 200 and "npm run build" in resp.text
