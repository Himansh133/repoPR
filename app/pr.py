"""Draft pull requests, created only after the user approves a run.

The changes come from the run's working copy, exactly as tested. They reach GitHub through
the REST API (blobs, one tree, one commit, a new branch, a draft PR), never `git push`, so
the write token stays in one Authorization header and never touches git config or a URL.
Nothing here merges, force-pushes or updates an existing branch.
"""

import base64
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from app.github import API_URL, TIMEOUT_S, GitHubClient, GitHubError
from app.patch import PatchError, _check_path

log = logging.getLogger(__name__)

MAX_PR_FILES = 15  # up to 3 patches of 5 files each
MAX_FILE_BYTES = 1_000_000
MAX_BODY_CHARS = 60_000  # GitHub's limit is 65,536
MAX_TITLE_CHARS = 120
GIT_TIMEOUT_S = 30
_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^repopilot/[A-Za-z0-9._-]{1,80}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_MODES = ("100644", "100755")


class PRError(Exception):
    """The draft PR cannot be created. Messages never contain the token."""


@dataclass(frozen=True)
class FileChange:
    path: str
    mode: str
    content: bytes


# --- the run's changes -----------------------------------------------------------------------

def _git(workdir: Path, *args: str) -> bytes:
    cmd = ["git", "-c", "core.hooksPath=/dev/null", *args]
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise PRError("git timed out") from None
    if proc.returncode != 0:
        raise PRError("git failed while reading the working copy")
    return proc.stdout


def collect_changes(
    workdir: Path, first_commit: str, last_commit: str
) -> tuple[str, list[FileChange]]:
    """Return (base commit, final contents of every changed file) for a run's working copy.

    Refuses if the copy moved on or has uncommitted edits since the tests ran, or if
    anything other than adding or modifying regular files happened.
    """
    workdir = Path(workdir)
    if not (workdir / ".git").exists():
        raise PRError("Working copy not found (was the run made with --cleanup?)")
    head = _git(workdir, "rev-parse", "HEAD").decode().strip()
    if head != last_commit:
        raise PRError("The working copy has changed since the tests ran")
    if _git(workdir, "status", "--porcelain").strip():
        raise PRError("The working copy has uncommitted changes")
    base = _git(workdir, "rev-parse", f"{first_commit}^").decode().strip()

    fields = _git(workdir, "diff", "--name-status", "--no-renames", "-z", base, last_commit)
    parts = [p.decode() for p in fields.split(b"\0") if p]
    entries = list(zip(parts[::2], parts[1::2], strict=True))
    if not entries:
        raise PRError("The run changed no files")
    if len(entries) > MAX_PR_FILES:
        raise PRError(f"Too many changed files for one PR (max {MAX_PR_FILES})")

    files = []
    for status, path in entries:
        if status not in ("A", "M"):
            raise PRError("A RepoPilot PR only adds or modifies files")
        try:
            _check_path(path)
        except PatchError as exc:
            raise PRError(str(exc)) from None
        mode = _git(workdir, "ls-tree", last_commit, "--", path).decode().split(" ", 1)[0]
        if mode not in _MODES:
            raise PRError(f"Only regular files can be included: {path}")
        content = _git(workdir, "show", f"{last_commit}:{path}")
        if len(content) > MAX_FILE_BYTES:
            raise PRError(f"File is too large for the PR: {path}")
        files.append(FileChange(path, mode, content))
    return base, files


# --- title and body ----------------------------------------------------------------------------

def pr_title(issue_number: int, issue_title: str) -> str:
    """One line, at most 120 characters."""
    return f"Fix #{issue_number}: {' '.join(issue_title.split())}"[:MAX_TITLE_CHARS]


def pr_body(issue_number: int, run_id: int, report: str, closes_issue: bool = False) -> str:
    """`Refs #N` by default; `Fixes #N` (closes the issue on merge) only when asked."""
    keyword = "Fixes" if closes_issue else "Refs"
    head = (
        f"{keyword} #{issue_number}\n\n"
        f"Draft opened by RepoPilot from run #{run_id} after manual approval. "
        "Review the diff and the test results before merging.\n\n"
        "<details><summary>RepoPilot report</summary>\n\n````text\n"
    )
    tail = "\n````\n\n</details>\n"
    room = MAX_BODY_CHARS - len(head) - len(tail)
    if len(report) > room:
        marker = "\n[report truncated]"
        report = report[: room - len(marker)] + marker
    return head + report + tail


# --- GitHub writes -------------------------------------------------------------------------------

class GitHubWriter:
    """Creates one branch, one commit and one draft PR. Needs Contents and Pull requests write."""

    def __init__(self, token: SecretStr, base_url: str = API_URL) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"GitHubWriter(base_url={self._base_url!r}, token=set)"

    async def create_draft_pr(
        self,
        owner: str,
        repo: str,
        branch: str,
        base_sha: str,
        files: list[FileChange],
        title: str,
        body: str,
    ) -> str:
        """Create the branch from `base_sha` with `files` in one commit, open a draft PR on
        the default branch, and return its URL. If the PR fails, the new branch is deleted."""
        if not _OWNER.match(owner) or not _REPO.match(repo) or repo in (".", ".."):
            raise PRError("Invalid repository name")
        if not _BRANCH.match(branch) or ".." in branch:
            raise PRError("Branch name must start with repopilot/")
        if not _SHA.match(base_sha):
            raise PRError("Base commit must be a full SHA")
        if not files or len(files) > MAX_PR_FILES:
            raise PRError("A PR needs between 1 and 15 changed files")
        for f in files:
            try:
                _check_path(f.path)
            except PatchError as exc:
                raise PRError(str(exc)) from None
            if f.mode not in _MODES:
                raise PRError(f"Only regular files can be included: {f.path}")

        root = f"/repos/{owner}/{repo}"
        info = await self._call("GET", root, missing_ok=True)
        if info is None:
            raise PRError(f"Repository {owner}/{repo} not found, or the write token cannot see it")
        if (info.get("permissions") or {}).get("push") is False:
            raise PRError("The write token has no write access to this repository")
        default_branch = info.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise PRError("Unexpected response shape from GitHub")

        base = await self._call("GET", f"{root}/git/commits/{base_sha}", missing_ok=True)
        if base is None:
            raise PRError(
                "The base commit is not on GitHub. Push your branch first, then approve again."
            )
        if await self._call("GET", f"{root}/git/ref/heads/{branch}", missing_ok=True) is not None:
            raise PRError(f"Branch {branch} already exists on GitHub; it is never overwritten")

        tree = []
        for f in files:
            blob = await self._call("POST", f"{root}/git/blobs", {
                "content": base64.b64encode(f.content).decode(), "encoding": "base64"})
            tree.append({"path": f.path, "mode": f.mode, "type": "blob", "sha": blob["sha"]})
        new_tree = await self._call(
            "POST", f"{root}/git/trees", {"base_tree": base["tree"]["sha"], "tree": tree})
        commit = await self._call("POST", f"{root}/git/commits", {
            "message": title, "tree": new_tree["sha"], "parents": [base_sha]})
        await self._call("POST", f"{root}/git/refs",
                         {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        try:
            pull = await self._call("POST", f"{root}/pulls", {
                "title": title, "head": branch, "base": default_branch, "body": body,
                "draft": True})
            return str(pull["html_url"])
        except (PRError, KeyError, TypeError) as exc:
            try:
                await self._call("DELETE", f"{root}/git/refs/heads/{branch}")
                cleanup = "the new branch was deleted"
            except PRError:
                cleanup = f"delete branch {branch} on GitHub by hand"
            raise PRError(f"Creating the pull request failed ({exc}); {cleanup}") from None

    async def _call(
        self, method: str, path: str, payload: dict | None = None, missing_ok: bool = False
    ) -> Any:
        """One API request. Every failure is a PRError; a 404 returns None if `missing_ok`."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self._token.get_secret_value()}",
        }
        log.debug("%s %s", method, path)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, base_url=self._base_url) as http:
                resp = await http.request(method, path, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise PRError("Request to GitHub timed out") from None
        except httpx.HTTPError as exc:
            raise PRError(f"Could not reach GitHub: {type(exc).__name__}") from None
        if resp.status_code == 404 and missing_ok:
            return None
        try:
            GitHubClient._raise_for_status(resp)
        except GitHubError as exc:
            raise PRError(str(exc)) from None
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise PRError("GitHub returned a body that is not JSON") from None
