"""Patch validation and a disposable working copy.

The model proposes a unified diff. This module parses it strictly, checks every path,
then applies it with `git apply` inside a temp clone on a new branch, so the user's own
checkout is never touched. Git runs as a fixed argument list (no shell), with hooks off.
"""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.paths import cache_dir

MAX_DIFF_BYTES = 20_000
MAX_PATCH_FILES = 5
GIT_TIMEOUT_S = 60
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_REJECTED_META = (
    "deleted file", "rename ", "similarity", "dissimilarity", "old mode", "new mode", "copy ",
)


def default_work_dir() -> Path:
    """Where temp clones live: ~/.cache/repopilot/work (honours XDG_CACHE_HOME).

    Not /tmp: Docker Desktop runs in a VM that shares the home folder but not /tmp,
    and the sandbox bind-mounts the working copy.
    """
    base = cache_dir() / "work"
    base.mkdir(parents=True, exist_ok=True)
    return base


class PatchError(Exception):
    """The patch (or working copy) is not acceptable. Messages are safe to show the model."""


@dataclass(frozen=True)
class FilePatch:
    path: str
    is_new: bool
    added: int
    removed: int


@dataclass(frozen=True)
class PatchResult:
    files: list[str]
    added: int
    removed: int
    branch: str
    commit_sha: str
    diff: str


def _check_path(path: str) -> None:
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise PatchError(f"Unsafe path in patch: {path[:100]!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PatchError(f"Unsafe path in patch: {path[:100]!r}")
    if ".git" in parts or parts[0] == ".github":
        raise PatchError(f"Patches may not touch {parts[0] if parts[0] == '.github' else '.git'}")


def _header_path(line: str) -> str:
    return line[4:].split("\t")[0].rstrip("\r")


def parse_diff(
    diff: str, max_bytes: int = MAX_DIFF_BYTES, max_files: int = MAX_PATCH_FILES
) -> list[FilePatch]:
    """Parse and validate a unified diff. Raises PatchError. Does not touch the filesystem.

    Allowed: modifying existing files and creating new ones. Rejected: deletes, renames,
    mode changes, binary patches, `..`/absolute/.git paths, anything malformed.
    """
    if not isinstance(diff, str) or not diff.strip():
        raise PatchError("Diff is empty")
    if len(diff.encode()) > max_bytes:
        raise PatchError(f"Diff is too large (max {max_bytes} bytes)")
    lines = diff.split("\n")
    while lines and lines[-1] == "":
        lines.pop()

    patches: list[FilePatch] = []
    i, n = 0, len(lines)

    def bad(msg: str) -> PatchError:
        return PatchError(f"Malformed diff at line {i + 1}: {msg}")

    while i < n:
        if lines[i].startswith("diff --git "):
            i += 1
            while i < n and not lines[i].startswith(("--- ", "diff --git ")):
                line = lines[i]
                if line.startswith(("GIT binary patch", "Binary files")):
                    raise PatchError("Binary patches are not allowed")
                if line.startswith(_REJECTED_META):
                    raise PatchError("Deletes, renames, copies and mode changes are not allowed")
                if not line.startswith(("new file mode ", "index ")):
                    raise bad("unexpected header line")
                i += 1
            if i >= n or lines[i].startswith("diff --git "):
                raise bad("file header (---/+++) missing")
        if not lines[i].startswith("--- "):
            raise bad("expected a '--- ' file header")
        old, new = _header_path(lines[i]), None
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise bad("expected a '+++ ' line")
        new = _header_path(lines[i])
        i += 1
        if new == "/dev/null":
            raise PatchError("Deleting files is not allowed")
        if not new.startswith("b/"):
            raise PatchError("File paths must start with a/ and b/")
        path = new[2:]
        is_new = old == "/dev/null"
        if not is_new:
            if not old.startswith("a/"):
                raise PatchError("File paths must start with a/ and b/")
            if old[2:] != path:
                raise PatchError("Renaming files is not allowed")
        _check_path(path)

        added = removed = hunks = 0
        while i < n and lines[i].startswith("@@"):
            m = _HUNK.match(lines[i])
            if not m:
                raise bad("bad hunk header")
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            while old_left > 0 or new_left > 0:
                if i >= n:
                    raise bad("hunk is shorter than its header says")
                tag = lines[i][:1]
                if tag == " ":
                    old_left, new_left = old_left - 1, new_left - 1
                elif tag == "-":
                    old_left, removed = old_left - 1, removed + 1
                elif tag == "+":
                    new_left, added = new_left - 1, added + 1
                else:
                    raise bad("line is not part of the hunk")
                if old_left < 0 or new_left < 0:
                    raise bad("hunk is longer than its header says")
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            hunks += 1
        if hunks == 0:
            raise bad("no hunks")
        patches.append(FilePatch(path=path, is_new=is_new, added=added, removed=removed))
        if len(patches) > max_files:
            raise PatchError(f"Too many files in one patch (max {max_files})")
        if i < n and not lines[i].startswith(("--- ", "diff --git ")):
            raise bad("unexpected line after hunk")
    if not patches:
        raise PatchError("Diff contains no file changes")
    return patches


class WorkingCopy:
    """A temp clone of a repo on a new branch. Patches are applied and committed here."""

    def __init__(self, root: Path, branch: str, base_sha: str) -> None:
        self.root = root
        self.branch = branch
        self.base_sha = base_sha

    @classmethod
    def create(
        cls, source: str | Path, issue_number: int, base_dir: Path | None = None
    ) -> "WorkingCopy":
        """Clone `source` (committed state only) into a temp dir and branch off it."""
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
            raise PatchError("Issue number must be a positive integer")
        parent = base_dir or default_work_dir()
        dest = Path(tempfile.mkdtemp(prefix="repopilot-", dir=parent)).resolve()
        branch = f"repopilot/issue-{issue_number}"
        try:
            _git(dest.parent, "clone", "--local", "--no-hardlinks", "--quiet", "--",
                 str(Path(source)), str(dest))
            _git(dest, "checkout", "-q", "-b", branch)
            base = _git(dest, "rev-parse", "HEAD").stdout.strip()
        except PatchError:
            shutil.rmtree(dest, ignore_errors=True)
            raise PatchError("Could not copy the repository (is it a git repo?)") from None
        return cls(dest, branch, base)

    def cleanup(self) -> None:
        """Delete the temp clone."""
        shutil.rmtree(self.root, ignore_errors=True)

    def apply_patch(self, diff: str) -> PatchResult:
        """Validate, check and apply a diff, then commit it. Changes nothing on failure."""
        patches = parse_diff(diff)
        self._check_filesystem(patches)
        data = (diff if diff.endswith("\n") else diff + "\n").encode()
        try:
            _git(self.root, "apply", "--check", "--whitespace=nowarn", "-", stdin=data)
        except PatchError as exc:
            raise PatchError(f"Patch does not apply: {exc}") from None
        paths = [p.path for p in patches]
        try:
            _git(self.root, "apply", "--whitespace=nowarn", "-", stdin=data)
            _git(self.root, "add", "--", *paths)
            message = f"RepoPilot: patch {', '.join(paths)}"[:72]
            _git(self.root, "commit", "-q", "-m", message, identity=True)
            sha = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        except PatchError:
            subprocess.run(["git", "reset", "-q", "--hard", "HEAD"], cwd=self.root)
            subprocess.run(["git", "clean", "-fdq"], cwd=self.root)
            raise PatchError("Applying the patch failed unexpectedly") from None
        return PatchResult(
            files=paths,
            added=sum(p.added for p in patches),
            removed=sum(p.removed for p in patches),
            branch=self.branch,
            commit_sha=sha,
            diff=diff,
        )

    def final_diff(self) -> str:
        """The full diff of the branch against where it started."""
        return _git(
            self.root, "diff", "--no-ext-diff", "--no-color", self.base_sha, "HEAD"
        ).stdout

    def _check_filesystem(self, patches: list[FilePatch]) -> None:
        for patch in patches:
            current = self.root
            for part in patch.path.split("/"):
                current = current / part
                if current.is_symlink():
                    raise PatchError(f"Path goes through a symlink: {patch.path}")
            if not current.resolve().is_relative_to(self.root):
                raise PatchError(f"Path resolves outside the repository: {patch.path}")
            if patch.is_new and current.exists():
                raise PatchError(f"File already exists: {patch.path}")
            if not patch.is_new and not current.is_file():
                raise PatchError(f"File not found in the repository: {patch.path}")


def _git(cwd: Path, *args: str, stdin: bytes | None = None, identity: bool = False):
    """Run git as a fixed argument list. Hooks are disabled. Raises PatchError on failure."""
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    if identity:
        cmd += ["-c", "user.name=RepoPilot", "-c", "user.email=repopilot@users.noreply.github.com"]
    try:
        proc = subprocess.run(
            [*cmd, *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        raise PatchError("git timed out") from None
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        first = (err.strip().splitlines() or ["git failed"])[0].replace(str(cwd), "<repo>")
        raise PatchError(first[:200])
    return subprocess.CompletedProcess(args, 0, out, err)
