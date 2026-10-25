"""Read-only GitHub client: fetch an issue (with comments) and basic repo metadata.

Everything returned here is untrusted text written by other people. It is capped in
size, and the token is sent only in the Authorization header, never logged.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
TIMEOUT_S = 15
MAX_BODY_CHARS = 10_000
MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 2_000
TRUNCATED = "\n[truncated]"
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubError(Exception):
    """Base class for GitHub client errors. Messages never contain the token."""


class GitHubNotFound(GitHubError):
    """The repository (or other resource) does not exist or is private."""


class IssueNotFound(GitHubNotFound):
    """The issue number does not exist in that repository."""


class NotAnIssueError(GitHubError):
    """The number refers to a pull request, not an issue."""


class GitHubRateLimit(GitHubError):
    """GitHub rate limit hit. `reset_at` is a Unix timestamp, if GitHub sent one."""

    def __init__(self, message: str, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubAuthError(GitHubError):
    """The token is missing, invalid, or lacks permission."""


@dataclass(frozen=True)
class IssueComment:
    author: str
    body: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    url: str
    author: str
    labels: list[str] = field(default_factory=list)
    comments: list[IssueComment] = field(default_factory=list)


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    description: str
    default_branch: str
    language: str
    clone_url: str


def _cap(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def _check_repo_args(owner: str, repo: str) -> None:
    if not isinstance(owner, str) or not _OWNER.match(owner):
        raise GitHubError("Invalid repository owner")
    if not isinstance(repo, str) or not _REPO.match(repo) or repo in {".", ".."}:
        raise GitHubError("Invalid repository name")


class GitHubClient:
    """Read-only client for the GitHub REST API."""

    def __init__(self, token: SecretStr | None = None, base_url: str = API_URL) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        token = "set" if self._token else "none"
        return f"GitHubClient(base_url={self._base_url!r}, token={token})"

    async def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        """Fetch an issue and its first comments. Pull requests are rejected."""
        _check_repo_args(owner, repo)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise GitHubError("Issue number must be a positive integer")
        base = f"/repos/{owner}/{repo}/issues/{number}"
        try:
            data = await self._get(base)
        except GitHubNotFound:
            raise IssueNotFound(f"Issue #{number} not found in {owner}/{repo}") from None
        if "pull_request" in data:
            raise NotAnIssueError(f"#{number} is a pull request, not an issue")
        raw_comments = await self._get(f"{base}/comments", params={"per_page": MAX_COMMENTS})
        try:
            return Issue(
                number=data["number"],
                title=data["title"],
                body=_cap(data.get("body"), MAX_BODY_CHARS),
                state=data["state"],
                url=data["html_url"],
                author=(data.get("user") or {}).get("login", "unknown"),
                labels=[label["name"] for label in data.get("labels", [])],
                comments=[
                    IssueComment(
                        author=(c.get("user") or {}).get("login", "unknown"),
                        body=_cap(c.get("body"), MAX_COMMENT_CHARS),
                    )
                    for c in raw_comments[:MAX_COMMENTS]
                ],
            )
        except (KeyError, TypeError):
            raise GitHubError("Unexpected response shape from GitHub") from None

    async def get_repo_info(self, owner: str, repo: str) -> RepoInfo:
        """Fetch basic repository metadata."""
        _check_repo_args(owner, repo)
        data = await self._get(f"/repos/{owner}/{repo}")
        try:
            return RepoInfo(
                full_name=data["full_name"],
                description=data.get("description") or "",
                default_branch=data["default_branch"],
                language=data.get("language") or "",
                clone_url=data["clone_url"],
            )
        except (KeyError, TypeError):
            raise GitHubError("Unexpected response shape from GitHub") from None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        log.debug("GET %s", path)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, base_url=self._base_url) as http:
                resp = await http.get(path, headers=headers, params=params)
        except httpx.TimeoutException:
            raise GitHubError("Request to GitHub timed out") from None
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub: {type(exc).__name__}") from None
        self._raise_for_status(resp)
        try:
            return resp.json()
        except ValueError:
            raise GitHubError("GitHub returned a body that is not JSON") from None

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        code = resp.status_code
        if code < 400:
            return
        remaining = resp.headers.get("x-ratelimit-remaining")
        if code == 429 or (code == 403 and remaining == "0"):
            reset = resp.headers.get("x-ratelimit-reset")
            raise GitHubRateLimit(
                "GitHub rate limit reached", int(reset) if reset and reset.isdigit() else None
            )
        if code == 401:
            raise GitHubAuthError("GitHub rejected the token (401)")
        if code == 403:
            raise GitHubAuthError("GitHub refused access (403): token lacks permission")
        if code == 404:
            raise GitHubNotFound("Not found on GitHub (or the repository is private)")
        raise GitHubError(f"GitHub request failed ({code})")
