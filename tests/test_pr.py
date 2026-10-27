"""Tests for app.pr: collecting a run's changes (real git) and the draft-PR writer (respx)."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.pr import FileChange, GitHubWriter, PRError, collect_changes, pr_body, pr_title

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
API = "https://api.github.com"
REPO = f"{API}/repos/octo/demo"
TOKEN = "github_pat_very_secret_write_token"
BASE = "a" * 40
BRANCH = "repopilot/issue-7-run-3"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


# --- collect_changes ---------------------------------------------------------------------

@pytest.fixture
def workdir(tmp_path):
    root = tmp_path / "work"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def commit_change(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(root, "add", "--", rel)
    git(root, "commit", "-q", "-m", f"change {rel}")
    return git(root, "rev-parse", "HEAD")


def test_collect_changes_returns_base_and_final_file_contents(workdir):
    base = git(workdir, "rev-parse", "HEAD")
    first = commit_change(workdir, "shop/cart.py", "x = 1\n")
    last = commit_change(workdir, "shop/new_helper.py", "y = 2\n")
    got_base, files = collect_changes(workdir, first, last)
    assert got_base == base
    assert files == [
        FileChange("shop/cart.py", "100644", b"x = 1\n"),
        FileChange("shop/new_helper.py", "100644", b"y = 2\n"),
    ]


def test_collect_changes_refuses_a_moved_head(workdir):
    first = commit_change(workdir, "shop/cart.py", "x = 1\n")
    commit_change(workdir, "README.md", "edited after the tests ran\n")
    with pytest.raises(PRError, match="changed since"):
        collect_changes(workdir, first, first)


def test_collect_changes_refuses_uncommitted_edits(workdir):
    first = commit_change(workdir, "shop/cart.py", "x = 1\n")
    (workdir / "shop" / "cart.py").write_text("tampered\n")
    with pytest.raises(PRError, match="uncommitted"):
        collect_changes(workdir, first, first)


def test_collect_changes_refuses_deletes(workdir):
    first = commit_change(workdir, "shop/cart.py", "x = 1\n")
    git(workdir, "rm", "-q", "README.md")
    git(workdir, "commit", "-q", "-m", "delete")
    last = git(workdir, "rev-parse", "HEAD")
    with pytest.raises(PRError, match="only adds or modifies"):
        collect_changes(workdir, first, last)


def test_collect_changes_needs_an_existing_git_working_copy(tmp_path):
    with pytest.raises(PRError, match="Working copy"):
        collect_changes(tmp_path / "gone", BASE, BASE)


# --- title and body ------------------------------------------------------------------------

def test_body_refs_the_issue_by_default_and_fixes_when_asked():
    body = pr_body(7, 3, "Status: PASSED\n...")
    assert body.startswith("Refs #7") and "Fixes #7" not in body
    assert "Status: PASSED" in body and "run #3" in body
    assert pr_body(7, 3, "r", closes_issue=True).startswith("Fixes #7")


def test_body_is_capped_for_github():
    body = pr_body(7, 3, "x" * 100_000)
    assert len(body) < 65_000 and "[report truncated]" in body


def test_title_is_short_and_single_line():
    title = pr_title(7, "Crash\non empty cart " + "y" * 300)
    assert "\n" not in title and len(title) <= 120 and title.startswith("Fix #7: Crash")


# --- GitHubWriter ----------------------------------------------------------------------------

FILES = [FileChange("shop/cart.py", "100644", b"x = 1\n")]


@pytest.fixture
def writer():
    return GitHubWriter(SecretStr(TOKEN))


def mock_happy_path(pr_status=201):
    respx.get(REPO).mock(return_value=httpx.Response(
        200, json={"default_branch": "main", "permissions": {"push": True}}))
    respx.get(f"{REPO}/git/commits/{BASE}").mock(
        return_value=httpx.Response(200, json={"sha": BASE, "tree": {"sha": "t" * 40}}))
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404))
    blob = respx.post(f"{REPO}/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "b" * 40}))
    tree = respx.post(f"{REPO}/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "n" * 40}))
    commit = respx.post(f"{REPO}/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "c" * 40}))
    ref = respx.post(f"{REPO}/git/refs").mock(return_value=httpx.Response(201, json={}))
    pull = respx.post(f"{REPO}/pulls").mock(return_value=httpx.Response(
        pr_status, json={"html_url": "https://github.com/octo/demo/pull/9"}))
    delete = respx.delete(f"{REPO}/git/refs/heads/{BRANCH}").mock(
        return_value=httpx.Response(204))
    return blob, tree, commit, ref, pull, delete


def sent(route, n=-1):
    return json.loads(route.calls[n].request.content)


@respx.mock
async def test_creates_one_commit_on_a_new_branch_and_a_draft_pr(writer):
    blob, tree, commit, ref, pull, delete = mock_happy_path()
    url = await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "Fix #7: x", "Refs #7")
    assert url == "https://github.com/octo/demo/pull/9"
    assert base64.b64decode(sent(blob)["content"]) == b"x = 1\n"
    assert sent(tree) == {"base_tree": "t" * 40, "tree": [
        {"path": "shop/cart.py", "mode": "100644", "type": "blob", "sha": "b" * 40}]}
    assert sent(commit)["parents"] == [BASE] and sent(commit)["tree"] == "n" * 40
    assert sent(ref) == {"ref": f"refs/heads/{BRANCH}", "sha": "c" * 40}
    assert sent(pull) == {"title": "Fix #7: x", "head": BRANCH, "base": "main",
                          "body": "Refs #7", "draft": True}
    assert not delete.called
    auth = pull.calls[0].request.headers["Authorization"]
    assert auth == f"Bearer {TOKEN}"


@respx.mock
async def test_failed_pr_deletes_the_new_branch(writer):
    *_, pull, delete = mock_happy_path(pr_status=422)
    with pytest.raises(PRError, match="branch was deleted") as exc:
        await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "t", "b")
    assert delete.called and TOKEN not in str(exc.value)


@respx.mock
async def test_base_commit_missing_on_github_stops_before_any_write(writer):
    respx.get(REPO).mock(return_value=httpx.Response(
        200, json={"default_branch": "main", "permissions": {"push": True}}))
    respx.get(f"{REPO}/git/commits/{BASE}").mock(return_value=httpx.Response(404))
    writes = respx.post(url__startswith=REPO).mock(return_value=httpx.Response(201, json={}))
    with pytest.raises(PRError, match="not on GitHub"):
        await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "t", "b")
    assert not writes.called


@respx.mock
async def test_existing_branch_is_never_overwritten(writer):
    respx.get(REPO).mock(return_value=httpx.Response(
        200, json={"default_branch": "main", "permissions": {"push": True}}))
    respx.get(f"{REPO}/git/commits/{BASE}").mock(
        return_value=httpx.Response(200, json={"sha": BASE, "tree": {"sha": "t" * 40}}))
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(200, json={}))
    writes = respx.post(url__startswith=REPO).mock(return_value=httpx.Response(201, json={}))
    with pytest.raises(PRError, match="already exists"):
        await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "t", "b")
    assert not writes.called


@respx.mock
async def test_token_without_push_access_stops_early(writer):
    respx.get(REPO).mock(return_value=httpx.Response(
        200, json={"default_branch": "main", "permissions": {"push": False}}))
    other = respx.route(url__startswith=f"{REPO}/").mock(return_value=httpx.Response(200))
    with pytest.raises(PRError, match="write access"):
        await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "t", "b")
    assert not other.called


@respx.mock
async def test_bad_token_error_does_not_leak_it(writer):
    respx.get(REPO).mock(return_value=httpx.Response(401))
    with pytest.raises(PRError) as exc:
        await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "t", "b")
    assert "401" in str(exc.value) and TOKEN not in str(exc.value)
    assert TOKEN not in repr(writer)


async def test_writer_rejects_bad_input_without_any_request(writer):
    with respx.mock(assert_all_called=False) as router:
        for owner, repo, branch, base, files in [
            ("../x", "demo", BRANCH, BASE, FILES),
            ("octo", "demo", "main", BASE, FILES),  # only repopilot/ branches
            ("octo", "demo", BRANCH, "not-a-sha", FILES),
            ("octo", "demo", BRANCH, BASE, []),
            ("octo", "demo", BRANCH, BASE, [FileChange("../etc/x", "100644", b"")]),
            ("octo", "demo", BRANCH, BASE, [FileChange(".github/workflows/x.yml", "100644", b"")]),
        ]:
            with pytest.raises(PRError):
                await writer.create_draft_pr(owner, repo, branch, base, files, "t", "b")
        assert not router.calls


@respx.mock
async def test_unknown_repo_is_a_clear_error(writer):
    respx.get(REPO).mock(return_value=httpx.Response(404))
    with pytest.raises(PRError, match="not found"):
        await writer.create_draft_pr("octo", "demo", BRANCH, BASE, FILES, "t", "b")
