"""Evaluation harness: run a benchmark of issues against several models and compare them.

Expectations (`expected_outcome`) live in the benchmark file and must be written before
running; the file's SHA-256 is saved with the results. Each run saves its patch, trace,
test results, latency, tokens and cost. Metrics are pure functions of the saved results.
"""

import hashlib
import json
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.agent.events import AgentEvent
from app.agent.fix import RunBudgets, RunResult, RunStatus, run_issue
from app.agent.investigate import LLM
from app.db import BudgetBlocked, Database, summarize_usage
from app.github import Issue
from app.patch import default_work_dir
from app.report import build_report
from app.sandbox import ExecStatus, Execution, Sandbox

MAX_ISSUES = 100
OUTCOMES = ("passed", "failed", "inconclusive")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
_GH = re.compile(r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_IGNORE = shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc")
GIT_TIMEOUT_S = 300


class EvalError(Exception):
    """The benchmark file or a repo it names is not usable."""


@dataclass(frozen=True)
class BenchIssue:
    id: str
    repo: str  # "dir:<path relative to the project>" or an https GitHub URL
    commit: str | None
    title: str
    body: str
    relevant_test: str
    expected_outcome: str
    notes: str


@dataclass(frozen=True)
class EvalResult:
    """One (model, issue) run, boiled down to what the metrics need."""

    model: str
    issue_id: str
    expected_outcome: str
    status: str  # passed | failed | inconclusive | aborted | error
    reason: str | None
    baseline_relevant_failed: bool
    final_passed: bool  # the last test run after a patch passed
    resolved: bool  # passed, and the relevant test was failing before
    patched: bool
    repairs_used: int
    steps: int
    input_tokens: int
    output_tokens: int
    cost: Decimal | None
    cost_estimated: bool
    latency_s: float
    tool_ok: int
    tool_total: int
    error: str | None
    run_id: int | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cost"] = None if self.cost is None else format(self.cost, "f")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EvalResult":
        d = dict(d)
        d["cost"] = None if d["cost"] is None else Decimal(d["cost"])
        return cls(**d)


@dataclass(frozen=True)
class Metrics:
    runs: int
    patch_test_pass_rate: float | None
    issue_resolution_rate: float | None
    tool_call_success_rate: float | None
    repair_success_rate: float | None
    cost_per_issue: Decimal | None
    unknown_cost_runs: int
    median_latency_s: float | None
    expectation_match_rate: float | None


# --- loading ------------------------------------------------------------------

def load_benchmark(path: str | Path) -> list[BenchIssue]:
    """Read and validate a benchmark JSONL file."""
    path = Path(path)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        raise EvalError(f"Benchmark file not found: {path}") from None
    issues: list[BenchIssue] = []
    seen: set[str] = set()
    for number, text in enumerate(lines, 1):
        if not text.strip():
            continue

        def bad(msg: str, n: int = number) -> EvalError:
            return EvalError(f"{path.name} line {n}: {msg}")

        try:
            data = json.loads(text)
        except ValueError:
            raise bad("not valid JSON") from None
        if not isinstance(data, dict):
            raise bad("each line must be a JSON object")
        for key in ("id", "repo", "issue", "relevant_test", "expected_outcome"):
            if key not in data:
                raise bad(f"missing field '{key}'")
        if not isinstance(data["id"], str) or not _ID.match(data["id"]):
            raise bad("invalid id (letters, digits, . _ - only)")
        if data["id"] in seen:
            raise bad(f"duplicate id '{data['id']}'")
        repo, commit = data["repo"], data.get("commit")
        if not isinstance(repo, str):
            raise bad("repo must be a string")
        if repo.startswith("dir:"):
            if not repo[4:]:
                raise bad("repo 'dir:' needs a path")
            if commit is not None:
                raise bad("commit must be null for dir: repos")
        elif _GH.match(repo):
            if not isinstance(commit, str) or not _SHA.match(commit):
                raise bad("commit must be 7-40 hex characters for GitHub repos")
        else:
            raise bad("repo must be dir:<path> or an https://github.com/<owner>/<repo> URL")
        issue = data["issue"]
        if not isinstance(issue, dict):
            raise bad("issue must be an object with title and body")
        if not isinstance(issue.get("title"), str) or not issue["title"].strip():
            raise bad("issue needs a non-empty title")
        if not isinstance(data["relevant_test"], str) or not data["relevant_test"].strip():
            raise bad("relevant_test must be a non-empty string")
        if data["expected_outcome"] not in OUTCOMES:
            raise bad(f"expected_outcome must be one of {', '.join(OUTCOMES)}")
        seen.add(data["id"])
        issues.append(
            BenchIssue(
                id=data["id"], repo=repo, commit=commit, title=issue["title"].strip(),
                body=str(issue.get("body") or ""), relevant_test=data["relevant_test"].strip(),
                expected_outcome=data["expected_outcome"], notes=str(data.get("notes") or ""),
            )
        )
        if len(issues) > MAX_ISSUES:
            raise bad(f"too many issues (max {MAX_ISSUES})")
    if not issues:
        raise EvalError("The benchmark file has no issues")
    return issues


def _git(*args: str) -> None:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        raise EvalError("git failed or timed out") from None
    if proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or ["git failed"])[0]
        raise EvalError(f"git failed: {first[:200]}")


def checkout_commit(url: str, commit: str, dest: Path) -> None:
    """Fetch one commit of a public GitHub repo into `dest` (fixed git commands, no shell)."""
    if not _GH.match(url):
        raise EvalError("Only https://github.com/<owner>/<repo> URLs can be fetched")
    if not _SHA.match(commit):
        raise EvalError("commit must be 7-40 hex characters")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", str(dest))
    _git("-C", str(dest), "remote", "add", "origin", url)
    _git("-C", str(dest), "fetch", "--depth", "1", "origin", commit)
    _git("-C", str(dest), "checkout", "-q", "FETCH_HEAD")


def prepare_repo(issue: BenchIssue, base_dir: Path) -> Path:
    """Make a fresh git repo for an issue in a temp folder. The caller removes `path.parent`."""
    holder = Path(tempfile.mkdtemp(prefix="eval-", dir=default_work_dir()))
    dest = holder / "repo"
    try:
        if issue.repo.startswith("dir:"):
            base = Path(base_dir).resolve()
            src = (base / issue.repo[4:]).resolve()
            if not src.is_relative_to(base):
                raise EvalError(f"Benchmark repo path is outside the project: {issue.repo}")
            if not src.is_dir():
                raise EvalError(f"Benchmark repo not found: {issue.repo}")
            shutil.copytree(src, dest, ignore=_IGNORE)
            _git("-C", str(dest), "init", "-q", "-b", "main")
            _git("-C", str(dest), "add", "-A")
            _git("-C", str(dest), "-c", "user.name=RepoPilot", "-c",
                 "user.email=repopilot@users.noreply.github.com", "commit", "-q", "-m", "seed")
        else:
            checkout_commit(issue.repo, issue.commit or "", dest)
    except Exception:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    return dest


# --- evaluating one run -------------------------------------------------------

def _relevant_failed(names: list[str], relevant: str) -> bool:
    return any(n == relevant or n.endswith(relevant) or relevant.endswith(n) for n in names)


def evaluate_run(
    run: RunResult,
    issue: BenchIssue,
    model: str,
    latency_s: float,
    cost: Decimal | None,
    cost_estimated: bool,
    run_id: int | None = None,
) -> EvalResult:
    """Boil a RunResult down to an EvalResult."""
    ok = total = 0
    for e in run.events:
        if e.kind == "tool_result":
            total += 1
            ok += bool(e.data.get("ok"))
        elif e.kind in ("plan_accepted", "patch_proposed"):
            total += 1
            ok += 1
        elif e.kind in ("plan_rejected", "patch_rejected"):
            total += 1
    baseline_hit = bool(run.baseline) and _relevant_failed(
        run.baseline.failed_tests, issue.relevant_test
    )
    return EvalResult(
        model=model,
        issue_id=issue.id,
        expected_outcome=issue.expected_outcome,
        status=run.status.value,
        reason=run.reason,
        baseline_relevant_failed=baseline_hit,
        final_passed=bool(run.executions) and run.executions[-1].status is ExecStatus.PASSED,
        resolved=run.status is RunStatus.PASSED and baseline_hit,
        patched=bool(run.patches),
        repairs_used=run.repairs_used,
        steps=run.steps,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost=cost,
        cost_estimated=cost_estimated,
        latency_s=latency_s,
        tool_ok=ok,
        tool_total=total,
        error=None,
        run_id=run_id,
    )


def _error_result(issue: BenchIssue, model: str, message: str, latency_s: float,
                  run_id: int | None) -> EvalResult:
    return EvalResult(
        model=model, issue_id=issue.id, expected_outcome=issue.expected_outcome, status="error",
        reason=message, baseline_relevant_failed=False, final_passed=False, resolved=False,
        patched=False, repairs_used=0, steps=0, input_tokens=0, output_tokens=0, cost=None,
        cost_estimated=False, latency_s=latency_s, tool_ok=0, tool_total=0, error=message,
        run_id=run_id,
    )


# --- metrics ------------------------------------------------------------------

def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def compute_metrics(results: list[EvalResult]) -> Metrics:
    """Metrics for one model's results.

    patch test-pass rate: of runs that made a patch, how many had the last test run pass.
    issue resolution rate: of all runs, how many passed with the relevant test fixed.
    tool-call success rate: successful tool results, plans and patches over all of them.
    repair success rate: of runs that needed a repair, how many ended passed.
    """
    patched = [r for r in results if r.patched]
    repaired = [r for r in results if r.repairs_used >= 1]
    known = [r.cost for r in results if r.cost is not None]
    tool_total = sum(r.tool_total for r in results)
    return Metrics(
        runs=len(results),
        patch_test_pass_rate=_rate(sum(r.final_passed for r in patched), len(patched)),
        issue_resolution_rate=_rate(sum(r.resolved for r in results), len(results)),
        tool_call_success_rate=_rate(sum(r.tool_ok for r in results), tool_total),
        repair_success_rate=_rate(sum(r.status == "passed" for r in repaired), len(repaired)),
        cost_per_issue=(sum(known, Decimal(0)) / len(known)) if known else None,
        unknown_cost_runs=sum(r.cost is None for r in results),
        median_latency_s=statistics.median(r.latency_s for r in results) if results else None,
        expectation_match_rate=_rate(
            sum(r.status == r.expected_outcome for r in results), len(results)
        ),
    )


def metrics_by_model(results: list[EvalResult]) -> dict[str, Metrics]:
    models = list(dict.fromkeys(r.model for r in results))
    return {m: compute_metrics([r for r in results if r.model == m]) for m in models}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _cost_text(cost: Decimal) -> str:
    """Rounded to 8 places, the precision costs are stored at, without trailing zeros."""
    text = format(cost.quantize(Decimal("0.00000001")), "f")
    return text.rstrip("0").rstrip(".")


def comparison_table(by_model: dict[str, Metrics]) -> str:
    """A markdown table, one row per model."""
    head = (
        "| Model | Runs | Resolved | Patch test pass | Tool-call success | Repair success "
        "| Cost / issue | Median latency | Matched expectation |"
    )
    rows = [head, "|---|---|---|---|---|---|---|---|---|"]
    for model, m in by_model.items():
        if m.cost_per_issue is None:
            cost = "unknown"
        else:
            cost = _cost_text(m.cost_per_issue)
            if m.unknown_cost_runs:
                cost += f" ({m.unknown_cost_runs} unknown)"
        latency = "n/a" if m.median_latency_s is None else f"{m.median_latency_s:.1f} s"
        rows.append(
            f"| {model} | {m.runs} | {_pct(m.issue_resolution_rate)} | "
            f"{_pct(m.patch_test_pass_rate)} | {_pct(m.tool_call_success_rate)} | "
            f"{_pct(m.repair_success_rate)} | {cost} | {latency} | "
            f"{_pct(m.expectation_match_rate)} |"
        )
    return "\n".join(rows) + "\n"


def _metrics_dict(m: Metrics) -> dict:
    d = asdict(m)
    d["cost_per_issue"] = None if m.cost_per_issue is None else _cost_text(m.cost_per_issue)
    return d


# --- running ------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:80] or "model"


def _exec_dict(e: Execution | None) -> dict | None:
    if e is None:
        return None
    return {"status": e.status.value, "exit_code": e.exit_code, "duration_s": e.duration_s,
            "failed_tests": e.failed_tests, "truncated": e.truncated, "reason": e.reason,
            "log_tail": e.log_tail}


def _events_list(events: list[AgentEvent]) -> list[dict]:
    return [{"ts": e.ts, "state": e.state, "kind": e.kind, "data": e.data} for e in events]


async def run_eval(
    issues: list[BenchIssue],
    models: list[str],
    llm_for: Callable[[str], LLM],
    sandbox_factory: Callable[[], Sandbox],
    out_dir: Path,
    budgets: RunBudgets,
    *,
    base_dir: Path | None = None,
    prices_for: Callable[[str], tuple[Decimal | None, Decimal | None]] = lambda model: (None, None),
    db: Database | None = None,
    spend_cap: Decimal | None = None,
    benchmark_path: Path | None = None,
    profile: str = "python-pytest",
) -> list[EvalResult]:
    """Run every issue against every model, one at a time, saving artifacts as it goes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_dir = Path(base_dir) if base_dir else Path.cwd()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[EvalResult] = []
    stopped: str | None = None

    for model in models:
        for index, issue in enumerate(issues, 1):
            if db is not None:
                try:
                    db.check_can_start(spend_cap, 10**9, priced=True, allow_unpriced=True)
                except BudgetBlocked as exc:
                    stopped = str(exc)
                    break
            results.append(
                await _run_one(
                    issue, index, model, llm_for, sandbox_factory, out_dir, budgets,
                    base_dir, prices_for(model), db, profile,
                )
            )
        if stopped:
            break

    by_model = metrics_by_model(results)
    (out_dir / "results.json").write_text(json.dumps([r.to_dict() for r in results], indent=2))
    (out_dir / "summary.json").write_text(
        json.dumps({m: _metrics_dict(v) for m, v in by_model.items()}, indent=2)
    )
    (out_dir / "comparison.md").write_text(comparison_table(by_model))
    bench_hash = (
        hashlib.sha256(Path(benchmark_path).read_bytes()).hexdigest() if benchmark_path else None
    )
    (out_dir / "meta.json").write_text(
        json.dumps(
            {
                "started_at": started_at,
                "models": models,
                "issues": [i.id for i in issues],
                "benchmark_sha256": bench_hash,
                "budgets": asdict(budgets),
                "stopped_reason": stopped,
            },
            indent=2,
        )
    )
    return results


async def _run_one(issue, index, model, llm_for, sandbox_factory, out_dir, budgets,
                   base_dir, prices, db, profile) -> EvalResult:
    folder = out_dir / _slug(model) / issue.id
    folder.mkdir(parents=True, exist_ok=True)
    run_id = db.start_run(issue.repo, index, issue.title, model) if db else None
    repo_path: Path | None = None
    workdir: Path | None = None
    started = time.monotonic()
    try:
        repo_path = prepare_repo(issue, base_dir)
        as_issue = Issue(number=index, title=issue.title, body=issue.body, state="open",
                         url="", author="eval")
        run = await run_issue(
            llm_for(model), repo_path, as_issue, sandbox_factory(), budgets, profile=profile
        )
        workdir = run.workdir
        latency = time.monotonic() - started
        usage = summarize_usage(run.events, prices)
        if db is not None:
            db.finish_run(run_id, run, build_report(run, as_issue), prices)
        result = evaluate_run(run, issue, model, latency, usage.total, usage.estimated, run_id)
        (folder / "patch.diff").write_text(run.final_diff)
        (folder / "trace.json").write_text(json.dumps(_events_list(run.events), default=str))
        (folder / "tests.json").write_text(
            json.dumps({"baseline": _exec_dict(run.baseline),
                        "after_patch": [_exec_dict(e) for e in run.executions]})
        )
    except Exception as exc:
        latency = time.monotonic() - started
        message = f"{type(exc).__name__}: {exc}"
        if db is not None:
            db.fail_run(run_id, message)
        result = _error_result(issue, model, message, latency, run_id)
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        if repo_path is not None:
            shutil.rmtree(repo_path.parent, ignore_errors=True)
    (folder / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
    return result


def load_results(out_dir: str | Path) -> list[EvalResult]:
    """Load the results saved by `run_eval`."""
    path = Path(out_dir) / "results.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise EvalError(f"No readable results.json in {out_dir}") from None
    return [EvalResult.from_dict(d) for d in data]
