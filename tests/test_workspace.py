"""Tests for app.workspace: safe root, path resolver, bounded reader, clone."""

import subprocess

import pytest

from app.workspace import (
    FileReadError,
    PathSafetyError,
    Workspace,
    WorkspaceError,
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')\n")
    (root / "README.md").write_text("hello\n")
    return root


@pytest.fixture
def ws(repo):
    return Workspace(repo, max_file_bytes=100)


# --- construction -----------------------------------------------------------

def test_root_must_exist_and_be_a_directory(tmp_path):
    with pytest.raises(WorkspaceError):
        Workspace(tmp_path / "nope")
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(WorkspaceError):
        Workspace(f)


# --- path resolver ----------------------------------------------------------

def test_resolve_valid_path(ws, repo):
    assert ws.resolve("src/a.py") == (repo / "src" / "a.py").resolve()


def test_resolve_allows_dot_segments_that_stay_inside(ws, repo):
    assert ws.resolve("src/./a.py") == (repo / "src" / "a.py").resolve()


@pytest.mark.parametrize(
    "bad",
    ["../secret.txt", "src/../../secret.txt", "..", "src/..", "a/../../b"],
)
def test_traversal_rejected(ws, bad):
    with pytest.raises(PathSafetyError):
        ws.resolve(bad)


def test_traversal_that_stays_inside_is_still_rejected(ws):
    # Simple rule: any ".." is refused, even if it would land inside the repo.
    with pytest.raises(PathSafetyError):
        ws.resolve("src/../README.md")


@pytest.mark.parametrize("bad", ["/etc/passwd", "/", "//etc/passwd"])
def test_absolute_path_rejected(ws, bad):
    with pytest.raises(PathSafetyError):
        ws.resolve(bad)


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "src/a.py\x00.txt"])
def test_empty_or_null_byte_rejected(ws, bad):
    with pytest.raises(PathSafetyError):
        ws.resolve(bad)


def test_non_string_rejected(ws):
    with pytest.raises(PathSafetyError):
        ws.resolve(None)  # type: ignore[arg-type]


def test_symlink_file_escape_rejected(ws, repo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    (repo / "link.txt").symlink_to(secret)
    with pytest.raises(PathSafetyError):
        ws.resolve("link.txt")
    with pytest.raises(PathSafetyError):
        ws.read_file("link.txt")


def test_symlink_directory_escape_rejected(ws, repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("nope")
    (repo / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSafetyError):
        ws.resolve("linkdir/x.txt")


def test_symlink_inside_repo_is_allowed(ws, repo):
    (repo / "alias.py").symlink_to(repo / "src" / "a.py")
    assert ws.read_file("alias.py").content == "print('a')\n"


# --- bounded reader ---------------------------------------------------------

def test_read_file_returns_content(ws):
    result = ws.read_file("README.md")
    assert result.path == "README.md"
    assert result.content == "hello\n"
    assert result.truncated is False
    assert result.size_bytes == 6


def test_read_file_truncates_large_file(ws, repo):
    (repo / "big.txt").write_text("x" * 500)
    result = ws.read_file("big.txt")
    assert result.truncated is True
    assert len(result.content) == 100
    assert result.size_bytes == 500


def test_read_file_exact_limit_is_not_truncated(ws, repo):
    (repo / "exact.txt").write_text("y" * 100)
    result = ws.read_file("exact.txt")
    assert result.truncated is False
    assert len(result.content) == 100


def test_read_file_truncation_in_middle_of_multibyte_char(ws, repo):
    (repo / "uni.txt").write_text("é" * 100)  # 200 bytes
    result = ws.read_file("uni.txt")
    assert result.truncated is True  # must not crash on a cut character


def test_binary_file_rejected(ws, repo):
    (repo / "image.bin").write_bytes(b"\x89PNG\r\n\x00\x00data")
    with pytest.raises(FileReadError, match="binary"):
        ws.read_file("image.bin")


def test_missing_file(ws):
    with pytest.raises(FileReadError, match="not found"):
        ws.read_file("nope.py")


def test_directory_is_not_a_file(ws):
    with pytest.raises(FileReadError, match="directory"):
        ws.read_file("src")


def test_read_file_error_does_not_leak_absolute_path(ws, repo):
    with pytest.raises(FileReadError) as exc:
        ws.read_file("nope.py")
    assert str(repo) not in str(exc.value)


# --- clone ------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc",
        "ext::sh -c evil",
        "--upload-pack=evil",
        "http://github.com/o/r",
        "https://evil.com/o/r",
        "https://github.com/o",
        "https://github.com/o/r/extra",
        "git@github.com:o/r.git",
        "https://github.com/o/r; rm -rf /",
        "",
    ],
)
def test_clone_rejects_bad_urls(tmp_path, url, monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(WorkspaceError):
        Workspace.clone(url, tmp_path / "dest")
    assert called == []


def test_clone_runs_fixed_git_command(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        (tmp_path / "dest").mkdir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ws = Workspace.clone("https://github.com/octocat/Hello-World.git", tmp_path / "dest")
    assert seen["cmd"] == [
        "git", "clone", "--depth", "1", "--",
        "https://github.com/octocat/Hello-World.git", str(tmp_path / "dest"),
    ]
    assert seen["kwargs"].get("shell") in (None, False)
    assert seen["kwargs"].get("timeout")
    assert ws.root == (tmp_path / "dest").resolve()


def test_clone_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 128, "", "fatal: not found"),
    )
    with pytest.raises(WorkspaceError, match="clone failed"):
        Workspace.clone("https://github.com/o/r", tmp_path / "dest")


def test_clone_refuses_existing_destination(tmp_path):
    (tmp_path / "dest").mkdir()
    with pytest.raises(WorkspaceError, match="exists"):
        Workspace.clone("https://github.com/o/r", tmp_path / "dest")
