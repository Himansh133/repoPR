"""What the CLI and the API share: starting a run, and approving or rejecting one.

Both front ends call these functions, so the checks (daily limits, repo rules, the approval
gate) live in one place. Errors are `ServiceError`s with a short message that is safe to
show and an HTTP-style status the API uses; the CLI prints the message and exits 5.
"""

import asyncio
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.events import AgentEvent
from app.agent.fix import RunBudgets, RunResult, run_issue
from app.agent.investigate import LLM
from app.config import Settings
from app.db import BudgetBlocked, Database, RunRecord
from app.github import GitHubClient, GitHubError, GitHubRateLimit, Issue
from app.llm import LLMClient
from app.patch import PatchError
from app.paths import cache_dir, default_db_path
from app.pr import FileChange, GitHubWriter, PRError, collect_changes, pr_body, pr_title
from app.report import build_report
from app.sandbox import Sandbox, default_profiles
from app.workspace import Workspace, WorkspaceError

_SLUG = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})$")
_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$"
)


class ServiceError(Exception):
    """A problem the user can fix. `status` is the HTTP status the API answers with."""

    status = 400


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class LimitReached(ServiceError):
    status = 429


class RunCrashed(Exception):
    """The agent raised unexpectedly. The run is already recorded as `error`."""


class LocalSettings(BaseSettings):
    """Only what commands without the model need, so they work without a GMI key."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    db_path: str | None = None
    github_write_token: SecretStr | None = None
    repopilot_api_key: SecretStr | None = None


def _default_clone(url: str, dest: Path) -> Path:
    return Workspace.clone(url, dest).root


def _default_sandbox(settings: Settings) -> Sandbox:
    profiles = default_profiles(
        settings.sandbox_image, settings.sandbox_timeout_s, settings.sandbox_node_image
    )
    return Sandbox(profiles=profiles)


@dataclass
class Deps:
    """Everything RepoPilot talks to. Replace pieces in tests."""

    settings: Callable[[], Settings] = Settings
    llm: Callable[[Settings], LLM] = LLMClient
    sandbox: Callable[[Settings], Sandbox] = _default_sandbox
    github: Callable[[Settings], GitHubClient] = field(
        default=lambda s: GitHubClient(token=s.github_token)
    )
    clone: Callable[[str, Path], Path] = _default_clone
    paths: Callable[[], LocalSettings] = LocalSettings
    writer: Callable[[SecretStr], GitHubWriter] = GitHubWriter
    ask: Callable[[str], str] = input


# --- settings, database, repos ---------------------------------------------------------------

def open_db(deps: Deps) -> Database:
    path = deps.paths().db_path
    return Database(Path(path) if path else default_db_path())


def load_settings(deps: Deps) -> Settings:
    """Settings, or one line naming what is missing or invalid (never a value)."""
    try:
        return deps.settings()
    except ValidationError as exc:
        missing = [str(e["loc"][0]).upper() for e in exc.errors() if e["type"] == "missing"]
        bad = [str(e["loc"][0]).upper() for e in exc.errors() if e["type"] != "missing"]
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if bad:
            parts.append(f"invalid {', '.join(bad)}")
        raise ServiceError(
            f"Configuration problem: {'; '.join(parts)}. Copy .env.example to .env and fill it in."
        ) from None


def parse_github_slug(text: str) -> tuple[str, str] | None:
    """`owner/repo`, an https GitHub URL, or an ssh URL -> (owner, repo), else None."""
    text = text.strip()
    match = _URL.match(text) or _SLUG.match(text)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo in (".", ".."):
        return None
    return owner, repo.removesuffix(".git")


def _local_slug(path: Path) -> tuple[str, str] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_github_slug(proc.stdout) if proc.returncode == 0 else None


def issue_from_fields(
    number: int, title: object, body: object = "", labels: object = None
) -> Issue:
    """An issue given directly (issue file or API body) instead of fetched from GitHub."""
    if not isinstance(title, str) or not title.strip():
        raise ServiceError("The issue needs a non-empty string 'title' (and optional 'body')")
    labels = labels if isinstance(labels, list) else []
    return Issue(
        number=number,
        title=title[:300],
        body=str(body or "")[:10_000],
        state="open",
        url="",
        author="local",
        labels=[str(x) for x in labels],
    )


# --- runs ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRequest:
    repo: str  # local git repo path or https://github.com/o/r
    issue: int
    github: str | None = None  # owner/repo, if it cannot be worked out from `repo`
    issue_given: Issue | None = None  # skips GitHub
    allow_unpriced: bool = False


@dataclass
class PreparedRun:
    run_id: int
    source: Path
    slug: tuple[str, str] | None
    clone_dir: Path | None
    issue: Issue

    def cleanup_clone(self) -> None:
        """Remove the temp clone of a GitHub URL (not the working copy)."""
        if self.clone_dir is not None:
            shutil.rmtree(self.clone_dir, ignore_errors=True)


def prepare_run(req: RunRequest, settings: Settings, deps: Deps, db: Database) -> PreparedRun:
    """Check the daily limits, resolve the repo, get the issue and record the run.

    The limits are checked first, before any clone, GitHub call or model call.
    """
    prices = (settings.price_in_per_m, settings.price_out_per_m)
    try:
        db.check_can_start(
            settings.daily_spend_cap,
            settings.max_runs_per_day,
            priced=all(p is not None for p in prices),
            allow_unpriced=settings.allow_unpriced or req.allow_unpriced,
        )
    except BudgetBlocked as exc:
        raise LimitReached(str(exc)) from None

    source, slug, clone_dir = _resolve_repo(req, deps)
    try:
        issue = req.issue_given or _get_issue(req.issue, slug, settings, deps)
        label = f"{slug[0]}/{slug[1]}" if slug else str(source)
        run_id = db.start_run(label, issue.number, issue.title, settings.gmi_model)
    except BaseException:
        if clone_dir is not None:
            shutil.rmtree(clone_dir, ignore_errors=True)
        raise
    return PreparedRun(run_id, source, slug, clone_dir, issue)


def execute_run(
    prep: PreparedRun,
    settings: Settings,
    deps: Deps,
    db: Database,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> tuple[RunResult, str, RunRecord]:
    """Run the agent for a prepared run and save it. Returns (result, report, record).

    A failure is recorded on the run before it is raised: ServiceError if the repo could
    not be prepared, RunCrashed for anything unexpected. The temp clone is always removed.
    """
    try:
        try:
            result = asyncio.run(
                run_issue(
                    deps.llm(settings),
                    prep.source,
                    prep.issue,
                    deps.sandbox(settings),
                    RunBudgets.from_settings(settings),
                    profile=settings.sandbox_profile,
                    on_event=on_event,
                )
            )
        except PatchError as exc:
            db.fail_run(prep.run_id, f"could not prepare the repository: {exc}")
            raise ServiceError(f"Could not prepare the repository: {exc}") from None
        except KeyboardInterrupt:
            db.fail_run(prep.run_id, "interrupted")
            raise
        except Exception as exc:  # a bug or an unexpected failure: record it, do not hide it
            db.fail_run(prep.run_id, f"{type(exc).__name__}: {exc}")
            raise RunCrashed(f"{type(exc).__name__}: {exc}") from None
        report = build_report(result, prep.issue)
        prices = (settings.price_in_per_m, settings.price_out_per_m)
        record = db.finish_run(prep.run_id, result, report, prices)
        return result, report, record
    finally:
        prep.cleanup_clone()


def _resolve_repo(req: RunRequest, deps: Deps) -> tuple[Path, tuple[str, str] | None, Path | None]:
    """Return (path to a local git repo, github slug or None, temp clone dir or None)."""
    explicit = parse_github_slug(req.github) if req.github else None
    if req.github and explicit is None:
        raise ServiceError("--github must look like owner/repo")

    if req.repo.startswith(("https://", "git@")):
        slug = parse_github_slug(req.repo)
        if slug is None or not req.repo.startswith("https://"):
            raise ServiceError("Only https://github.com/<owner>/<repo> URLs are supported")
        clone_dir = cache_dir() / "clones" / uuid.uuid4().hex[:12]
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            path = deps.clone(req.repo, clone_dir)
        except WorkspaceError as exc:
            shutil.rmtree(clone_dir, ignore_errors=True)
            raise ServiceError(f"Could not clone: {exc}") from None
        return Path(path), explicit or slug, clone_dir

    path = Path(req.repo).expanduser()
    if not path.is_dir():
        raise ServiceError(f"Repository path not found: {req.repo}")
    if not (path / ".git").exists():
        raise ServiceError("The repository path is not a git repository")
    return path, explicit or _local_slug(path), None


def _get_issue(number: int, slug, settings: Settings, deps: Deps) -> Issue:
    if slug is None:
        raise ServiceError("Cannot tell which GitHub repo this is; pass --github owner/repo")
    try:
        return asyncio.run(deps.github(settings).get_issue(slug[0], slug[1], number))
    except GitHubRateLimit as exc:
        reset = f" (resets at Unix time {exc.reset_at})" if exc.reset_at else ""
        raise ServiceError(f"{exc}{reset}") from None
    except GitHubError as exc:
        raise ServiceError(str(exc)) from None


# --- approve / reject ------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalPlan:
    """Everything a draft PR needs, checked. Nothing has been sent to GitHub yet."""

    run: RunRecord
    owner: str
    repo: str
    token: SecretStr
    base_sha: str
    files: list[FileChange]
    branch: str
    closes_issue: bool

    @property
    def keyword(self) -> str:
        return "Fixes" if self.closes_issue else "Refs"


def _undecided_run(db: Database, run_id: int) -> RunRecord:
    run = db.get_run(run_id)
    if run is None:
        raise NotFound(f"No run with id {run_id}")
    if run.pr_url:
        raise Conflict(f"Run #{run_id} already has a draft PR: {run.pr_url}")
    return run


def plan_approval(
    db: Database, deps: Deps, run_id: int, closes_issue: bool = False
) -> ApprovalPlan:
    """Run every approval check. Raises ServiceError; makes no GitHub call."""
    run = _undecided_run(db, run_id)
    if any(a["decision"] == "rejected" for a in db.get_approvals(run.id)):
        raise Conflict(f"Run #{run.id} was rejected; start a new run instead")
    if run.status != "passed":
        raise Conflict(f"Only passed runs can be approved (run #{run.id} is {run.status})")
    slug = parse_github_slug(run.repo)
    if slug is None:
        raise ServiceError(f"Run #{run.id} is not linked to a GitHub repo (it ran on a local "
                           "path with no GitHub origin remote)")
    token = deps.paths().github_write_token
    if token is None:
        raise ServiceError("Set GITHUB_WRITE_TOKEN (fine-grained; Contents and Pull requests "
                           "read/write on this repo only) to open a PR")
    patches = db.get_patches(run.id)
    if not patches:
        raise ServiceError(f"Run #{run.id} has no patch")
    try:
        base, files = collect_changes(
            Path(run.workdir or ""), patches[0]["commit_sha"], patches[-1]["commit_sha"]
        )
    except PRError as exc:
        raise ServiceError(str(exc)) from None
    branch = f"repopilot/issue-{run.issue_number}-run-{run.id}"
    return ApprovalPlan(run, slug[0], slug[1], token, base, files, branch, closes_issue)


def open_draft_pr(db: Database, deps: Deps, plan: ApprovalPlan, note: str = "") -> str:
    """Record the approval, then create the draft PR and save its URL."""
    run = plan.run
    db.record_approval(run.id, "approved", note)
    title = pr_title(run.issue_number, run.issue_title)
    body = pr_body(run.issue_number, run.id, run.report or "", plan.closes_issue)
    try:
        url = asyncio.run(deps.writer(plan.token).create_draft_pr(
            plan.owner, plan.repo, plan.branch, plan.base_sha, plan.files, title, body))
    except PRError as exc:
        error = ServiceError(f"Approved, but the draft PR was not created: {exc}")
        error.status = 502
        raise error from None
    db.set_pr_url(run.id, url)
    return url


def reject_run(db: Database, run_id: int, note: str = "") -> RunRecord:
    """Record a rejection. Nothing is sent to GitHub."""
    run = _undecided_run(db, run_id)
    db.record_approval(run.id, "rejected", note)
    return run
