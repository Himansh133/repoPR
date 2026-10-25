# GitHub issue fetching (read-only)

## What it does
`app/github.py` has `GitHubClient` with:
- `get_issue(owner, repo, number)` returns `Issue` (title, body, labels, author, state, url, first 20 comments).
- `get_repo_info(owner, repo)` returns `RepoInfo` (full name, description, default branch, language, clone URL).

## How to use
```python
gh = GitHubClient(token=settings.github_token)   # token is optional for public repos
issue = await gh.get_issue("octo", "demo", 7)
```

## Design notes
- Read-only: only GET requests. Use a fine-grained token with read access.
- Issue and comment text is untrusted. Body is capped at 10,000 characters, each comment at 2,000, and only 20 comments are fetched. Capped text ends with `[truncated]`.
- Owner, repo and number are validated before any request, so they cannot change the URL path.
- Pull requests are rejected (`NotAnIssueError`) because the issues endpoint also returns them.
- Errors: `IssueNotFound`, `GitHubNotFound`, `GitHubRateLimit` (with `reset_at`), `GitHubAuthError`, all under `GitHubError`. Timeouts, server errors and non-JSON bodies also raise `GitHubError`. No retries here.
- The token goes only in the `Authorization` header. It is never logged, never in error text, and not in `repr`.

## Limits
- No pagination beyond the first 20 comments.
- No retry on rate limit; the caller decides.

## How it is tested
`tests/test_github.py` mocks all HTTP with `respx`: success, no token, null body, size caps, 404, pull request, 403/429 rate limit, 401, plain 403, 500, timeout, bad JSON, repo info, bad arguments (no request sent), and token never in logs or errors.
