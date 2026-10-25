"""Tests for app.github.GitHubClient. All HTTP is mocked with respx."""

import logging

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimit,
    IssueNotFound,
    NotAnIssueError,
)

API = "https://api.github.com"
ISSUE_URL = f"{API}/repos/octo/demo/issues/7"
COMMENTS_URL = f"{ISSUE_URL}/comments"
REPO_URL = f"{API}/repos/octo/demo"
TOKEN = "ghp_supersecrettoken"


@pytest.fixture
def client():
    return GitHubClient(token=SecretStr(TOKEN))


def issue_json(**over):
    data = {
        "number": 7,
        "title": "Crash on empty list",
        "body": "Calling total([]) crashes.",
        "state": "open",
        "html_url": "https://github.com/octo/demo/issues/7",
        "labels": [{"name": "bug"}, {"name": "good first issue"}],
        "user": {"login": "alice"},
    }
    data.update(over)
    return data


def comments_json():
    return [
        {"user": {"login": "bob"}, "body": "I can reproduce this."},
        {"user": {"login": "carol"}, "body": "Maybe related to #3."},
    ]


@respx.mock
async def test_get_issue_success(client):
    issue = respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_json()))
    respx.get(COMMENTS_URL).mock(return_value=httpx.Response(200, json=comments_json()))
    result = await client.get_issue("octo", "demo", 7)
    assert result.number == 7
    assert result.title == "Crash on empty list"
    assert result.body == "Calling total([]) crashes."
    assert result.labels == ["bug", "good first issue"]
    assert result.author == "alice"
    assert [c.author for c in result.comments] == ["bob", "carol"]
    assert result.comments[0].body == "I can reproduce this."
    assert issue.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-github-api-version" in issue.calls.last.request.headers


@respx.mock
async def test_no_token_sends_no_auth_header():
    route = respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_json()))
    respx.get(COMMENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    await GitHubClient().get_issue("octo", "demo", 7)
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_null_body_becomes_empty_string(client):
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_json(body=None)))
    respx.get(COMMENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    assert (await client.get_issue("octo", "demo", 7)).body == ""


@respx.mock
async def test_long_body_and_comments_are_capped(client):
    respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(200, json=issue_json(body="x" * 50_000))
    )
    many = [{"user": {"login": "u"}, "body": "y" * 9_000} for _ in range(50)]
    respx.get(COMMENTS_URL).mock(return_value=httpx.Response(200, json=many))
    result = await client.get_issue("octo", "demo", 7)
    assert len(result.body) < 12_000
    assert result.body.endswith("[truncated]")
    assert len(result.comments) == 20
    assert all(len(c.body) < 3_000 for c in result.comments)


@respx.mock
async def test_missing_issue(client):
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(IssueNotFound):
        await client.get_issue("octo", "demo", 7)


@respx.mock
async def test_pull_request_is_rejected(client):
    respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(200, json=issue_json(pull_request={"url": "x"}))
    )
    with pytest.raises(NotAnIssueError):
        await client.get_issue("octo", "demo", 7)


@respx.mock
async def test_rate_limit_403_with_zero_remaining(client):
    respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"},
        )
    )
    with pytest.raises(GitHubRateLimit) as exc:
        await client.get_issue("octo", "demo", 7)
    assert exc.value.reset_at == 1893456000


@respx.mock
async def test_rate_limit_429(client):
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(429, json={"message": "slow"}))
    with pytest.raises(GitHubRateLimit):
        await client.get_issue("octo", "demo", 7)


@respx.mock
async def test_bad_token_401(client):
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubAuthError):
        await client.get_issue("octo", "demo", 7)


@respx.mock
async def test_403_without_rate_limit_is_auth_error(client):
    respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(
            403, json={"message": "Forbidden"}, headers={"x-ratelimit-remaining": "50"}
        )
    )
    with pytest.raises(GitHubAuthError):
        await client.get_issue("octo", "demo", 7)


@respx.mock
async def test_server_error_and_timeout_and_bad_json(client):
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(GitHubError):
        await client.get_issue("octo", "demo", 7)
    respx.get(ISSUE_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(GitHubError):
        await client.get_issue("octo", "demo", 7)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(GitHubError):
        await client.get_issue("octo", "demo", 7)


@respx.mock
async def test_get_repo_info(client):
    respx.get(REPO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "octo/demo",
                "description": "A demo",
                "default_branch": "main",
                "language": "Python",
                "clone_url": "https://github.com/octo/demo.git",
                "private": False,
            },
        )
    )
    info = await client.get_repo_info("octo", "demo")
    assert info.full_name == "octo/demo"
    assert info.default_branch == "main"
    assert info.language == "Python"
    assert info.description == "A demo"
    assert info.clone_url == "https://github.com/octo/demo.git"


@respx.mock
async def test_missing_repo(client):
    respx.get(REPO_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubNotFound):
        await client.get_repo_info("octo", "demo")


@pytest.mark.parametrize(
    "owner,repo,number",
    [
        ("../etc", "demo", 1),
        ("octo", "demo/../../x", 1),
        ("octo", "de mo", 1),
        ("", "demo", 1),
        ("octo", "demo", 0),
        ("octo", "demo", -3),
        ("octo", "demo", "7; DROP"),
    ],
)
async def test_bad_arguments_rejected_before_any_request(client, owner, repo, number):
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(GitHubError):
            await client.get_issue(owner, repo, number)  # type: ignore[arg-type]
        assert len(mock.calls) == 0


@respx.mock
async def test_token_never_logged_or_in_errors(client, caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubAuthError) as exc:
        await client.get_issue("octo", "demo", 7)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(client)
