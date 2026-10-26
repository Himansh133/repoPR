"""Tests for app.patch: diff parsing/validation and the working copy. Uses real git in tmp."""

import difflib
import shutil
import subprocess
from pathlib import Path

import pytest

from app.patch import PatchError, WorkingCopy, parse_diff

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    return subprocess.run(
        [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_diff(path, old, new, new_file=False):
    a = "/dev/null" if new_file else f"a/{path}"
    return "".join(
        difflib.unified_diff(
            [] if new_file else old.splitlines(True), new.splitlines(True), a, f"b/{path}"
        )
    )


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def wc(source):
    copy = WorkingCopy.create(source, 7)
    yield copy
    copy.cleanup()


CART = (FIXTURE / "shop" / "cart.py").read_text()
FIXED = CART.replace(
    "    return sum(prices) / len(prices)",
    "    if not prices:\n        return 0\n    return sum(prices) / len(prices)",
)
GOOD = make_diff("shop/cart.py", CART, FIXED)


# --- working copy --------------------------------------------------------------

def test_create_makes_separate_clone_on_new_branch(source):
    copy = WorkingCopy.create(source, 7)
    try:
        assert copy.root != source.resolve()
        assert copy.root.is_dir() and (copy.root / "shop" / "cart.py").exists()
        assert copy.branch == "repopilot/issue-7"
        assert git(copy.root, "rev-parse", "--abbrev-ref", "HEAD") == "repopilot/issue-7"
        assert copy.base_sha == git(source, "rev-parse", "HEAD")
    finally:
        copy.cleanup()


def test_create_from_non_git_directory_fails(tmp_path):
    with pytest.raises(PatchError):
        WorkingCopy.create(tmp_path, 1)


def test_create_rejects_bad_issue_number(source):
    for bad in (0, -1, "7; rm", None, True):
        with pytest.raises(PatchError):
            WorkingCopy.create(source, bad)  # type: ignore[arg-type]


def test_cleanup_removes_the_copy(source):
    copy = WorkingCopy.create(source, 1)
    copy.cleanup()
    assert not copy.root.exists()


# --- parse_diff: good input ------------------------------------------------------

def test_parse_good_diff():
    (fp,) = parse_diff(GOOD)
    assert fp.path == "shop/cart.py"
    assert fp.is_new is False
    assert (fp.added, fp.removed) == (2, 0)


def test_parse_new_file():
    diff = make_diff("shop/extra.py", "", "x = 1\ny = 2\n", new_file=True)
    (fp,) = parse_diff(diff)
    assert fp.is_new is True and fp.path == "shop/extra.py"
    assert (fp.added, fp.removed) == (2, 0)


def test_parse_git_style_headers_and_multiple_files():
    d2 = make_diff("README.md", "# Sample shop\n", "# Sample shop!\n")
    diff = (
        "diff --git a/shop/cart.py b/shop/cart.py\nindex 111..222 100644\n" + GOOD
        + "diff --git a/README.md b/README.md\n" + d2
    )
    assert [f.path for f in parse_diff(diff)] == ["shop/cart.py", "README.md"]


def test_parse_counts_removed_lines_and_no_newline_marker():
    diff = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n-old\n+new\n same\n"
        "\\ No newline at end of file\n"
    )
    (fp,) = parse_diff(diff)
    assert (fp.added, fp.removed) == (1, 1)


def test_removed_line_that_looks_like_a_header_is_not_a_new_file():
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,1 @@\n--- not a header\n keep\n"
    (fp,) = parse_diff(diff)
    assert fp.removed == 1


def test_parse_accepts_missing_final_newline_in_input():
    assert parse_diff(GOOD.rstrip("\n"))[0].path == "shop/cart.py"


# --- parse_diff: bad input ---------------------------------------------------------

@pytest.mark.parametrize(
    "diff",
    [
        "",
        "   \n",
        "hello, this is not a diff",
        "--- a/f.txt\n+++ b/f.txt\n",  # no hunk
        "--- a/f.txt\n@@ -1 +1 @@\n-a\n+b\n",  # missing +++
        "--- a/f.txt\n+++ b/f.txt\n@@ bad header @@\n-a\n+b\n",
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n-a\n+b\n",  # truncated hunk
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+b\nsome trailing prose\n",
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\nnot-a-diff-line\n",
        "Here is the fix:\n" + GOOD,
    ],
)
def test_malformed_diffs_rejected(diff):
    with pytest.raises(PatchError):
        parse_diff(diff)


@pytest.mark.parametrize(
    "path",
    ["../x.py", "shop/../../x.py", "/etc/passwd", ".git/config", "shop/.git/x",
     ".github/workflows/ci.yml", "a\\b.py", "a\x00b.py"],
)
def test_unsafe_paths_rejected(path):
    diff = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(PatchError):
        parse_diff(diff)


def test_headers_without_a_b_prefix_rejected():
    with pytest.raises(PatchError):
        parse_diff("--- /etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError):
        parse_diff("--- x.py\n+++ x.py\n@@ -1 +1 @@\n-a\n+b\n")


@pytest.mark.parametrize(
    "diff",
    [
        "--- a/f.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n",  # delete
        "diff --git a/f.txt b/f.txt\ndeleted file mode 100644\n--- a/f.txt\n+++ /dev/null\n",
        "diff --git a/f.txt b/g.txt\nsimilarity index 100%\nrename from f.txt\nrename to g.txt\n",
        "--- a/f.txt\n+++ b/g.txt\n@@ -1 +1 @@\n-a\n+b\n",  # path change = rename
        "diff --git a/f.txt b/f.txt\nold mode 100644\nnew mode 100755\n",
        "diff --git a/f.bin b/f.bin\nGIT binary patch\nliteral 3\nKcmZ\n",
        "diff --git a/f.bin b/f.bin\nBinary files a/f.bin and b/f.bin differ\n",
    ],
)
def test_deletes_renames_mode_changes_and_binary_rejected(diff):
    with pytest.raises(PatchError):
        parse_diff(diff)


def test_oversized_diff_rejected():
    big = make_diff("f.txt", "a\n", "b\n" * 20_000)
    assert len(big) > 20_000
    with pytest.raises(PatchError, match="too large"):
        parse_diff(big)


def test_too_many_files_rejected():
    diff = "".join(make_diff(f"f{i}.txt", "a\n", "b\n") for i in range(6))
    with pytest.raises(PatchError, match="files"):
        parse_diff(diff)


def test_limits_are_configurable():
    parse_diff(GOOD, max_bytes=100_000)
    with pytest.raises(PatchError):
        parse_diff(GOOD, max_bytes=50)


# --- apply -------------------------------------------------------------------------

def test_apply_good_patch(wc, source):
    result = wc.apply_patch(GOOD)
    assert result.files == ["shop/cart.py"]
    assert (result.added, result.removed) == (2, 0)
    assert result.branch == "repopilot/issue-7"
    assert "if not prices" in (wc.root / "shop" / "cart.py").read_text()
    assert git(wc.root, "rev-parse", "HEAD") == result.commit_sha != wc.base_sha
    assert git(wc.root, "status", "--porcelain") == ""
    assert "if not prices" not in (source / "shop" / "cart.py").read_text()  # source untouched


def test_final_diff_is_against_base(wc):
    wc.apply_patch(GOOD)
    final = wc.final_diff()
    assert "+    if not prices:" in final
    assert final.startswith("diff --git a/shop/cart.py")


def test_diff_without_trailing_newline_still_applies(wc):
    wc.apply_patch(GOOD.rstrip("\n"))
    assert "if not prices" in (wc.root / "shop" / "cart.py").read_text()


def test_patch_that_does_not_apply_changes_nothing(wc):
    wrong = make_diff("shop/cart.py", CART.replace("average", "mean"), FIXED)
    head = git(wc.root, "rev-parse", "HEAD")
    with pytest.raises(PatchError, match="does not apply"):
        wc.apply_patch(wrong)
    assert git(wc.root, "rev-parse", "HEAD") == head
    assert git(wc.root, "status", "--porcelain") == ""


def test_modifying_a_missing_file_fails(wc):
    with pytest.raises(PatchError, match="not found"):
        wc.apply_patch(make_diff("shop/ghost.py", "a\n", "b\n"))


def test_create_new_file_in_new_folder(wc):
    diff = make_diff("shop/utils/helpers.py", "", "def f():\n    return 1\n", new_file=True)
    result = wc.apply_patch(diff)
    assert (wc.root / "shop" / "utils" / "helpers.py").read_text() == "def f():\n    return 1\n"
    assert result.files == ["shop/utils/helpers.py"]


def test_creating_a_file_that_already_exists_fails(wc):
    with pytest.raises(PatchError, match="already exists"):
        wc.apply_patch(make_diff("shop/cart.py", "", "x\n", new_file=True))


def test_second_patch_applies_on_top_of_first(wc):
    wc.apply_patch(GOOD)
    current = (wc.root / "README.md").read_text()
    second = make_diff("README.md", current, current + "More notes.\n")
    wc.apply_patch(second)
    final = wc.final_diff()
    assert "if not prices" in final and "More notes." in final


def test_tests_may_be_patched(wc):
    path = "tests/test_cart.py"
    current = (wc.root / path).read_text()
    wc.apply_patch(make_diff(path, current, current + "\n\ndef test_extra():\n    assert True\n"))
    assert "test_extra" in (wc.root / path).read_text()


def test_path_through_symlink_rejected(source):
    outside = source.parent / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("a\n")
    (source / "linkdir").symlink_to(outside, target_is_directory=True)
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "add link")
    copy = WorkingCopy.create(source, 2)
    try:
        with pytest.raises(PatchError, match="symlink"):
            copy.apply_patch(make_diff("linkdir/x.py", "a\n", "b\n"))
        assert (outside / "x.py").read_text() == "a\n"
    finally:
        copy.cleanup()


def test_symlink_file_target_rejected(source):
    (source / "alias.py").symlink_to(source / "shop" / "cart.py")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "add alias")
    copy = WorkingCopy.create(source, 3)
    try:
        with pytest.raises(PatchError, match="symlink"):
            copy.apply_patch(make_diff("alias.py", CART, FIXED))
    finally:
        copy.cleanup()


def test_git_hooks_in_the_copy_are_not_run(wc):
    marker = wc.root.parent / "hook-ran"
    hook = wc.root / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    wc.apply_patch(GOOD)
    assert not marker.exists()


def test_error_messages_do_not_leak_absolute_paths(wc):
    with pytest.raises(PatchError) as exc:
        wc.apply_patch(make_diff("shop/ghost.py", "a\n", "b\n"))
    assert str(wc.root) not in str(exc.value)
