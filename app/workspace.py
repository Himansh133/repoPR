"""A local repository checkout with a safe root.

Every file access goes through `Workspace.resolve`, which refuses absolute paths,
`..`, null bytes and symlinks that point outside the repo. Reads are size-bounded.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_MAX_FILE_BYTES = 50_000
CLONE_TIMEOUT_S = 120
BINARY_SNIFF_BYTES = 8192
_GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class WorkspaceError(Exception):
    """Base class for workspace errors. Messages are safe to show to the model."""


class PathSafetyError(WorkspaceError):
    """The requested path is not allowed (traversal, absolute, symlink escape)."""


class FileReadError(WorkspaceError):
    """The file cannot be read (missing, a directory, or binary)."""


@dataclass(frozen=True)
class ReadResult:
    """Bounded file content. `size_bytes` is the real size on disk."""

    path: str
    content: str
    truncated: bool
    size_bytes: int


class Workspace:
    """A repository checkout. All file access stays inside `root`."""

    def __init__(self, root: str | Path, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> None:
        path = Path(root)
        if not path.is_dir():
            raise WorkspaceError("Workspace root does not exist or is not a directory")
        self.root = path.resolve()
        self.max_file_bytes = max_file_bytes

    @classmethod
    def clone(cls, url: str, dest: str | Path, **kwargs) -> "Workspace":
        """Shallow-clone a public GitHub repo into `dest` and return a Workspace.

        Only `https://github.com/<owner>/<repo>[.git]` is accepted. The git command
        is a fixed argument list (no shell), and `--` stops URL-as-flag tricks.
        """
        clean = url[:-4] if url.endswith(".git") else url
        if not _GITHUB_URL.match(clean):
            raise WorkspaceError("Only https://github.com/<owner>/<repo> URLs can be cloned")
        dest = Path(dest)
        if dest.exists():
            raise WorkspaceError("Clone destination already exists")
        cmd = ["git", "clone", "--depth", "1", "--", url, str(dest)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_S,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            raise WorkspaceError("git clone timed out") from None
        if proc.returncode != 0:
            raise WorkspaceError(f"git clone failed: {proc.stderr.strip()[:200]}")
        return cls(dest, **kwargs)

    def resolve(self, rel_path: str) -> Path:
        """Return the real absolute path for a repo-relative path, or raise PathSafetyError."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise PathSafetyError("Path must be a non-empty string")
        if "\x00" in rel_path:
            raise PathSafetyError("Path contains a null byte")
        if os.path.isabs(rel_path):
            raise PathSafetyError("Absolute paths are not allowed")
        if ".." in PurePosixPath(rel_path).parts:
            raise PathSafetyError("Path must not contain '..'")
        real = (self.root / rel_path).resolve()
        if not real.is_relative_to(self.root):
            raise PathSafetyError("Path resolves outside the repository")
        return real

    def read_file(self, rel_path: str) -> ReadResult:
        """Read a text file, returning at most `max_file_bytes` bytes of content."""
        real = self.resolve(rel_path)
        if not real.exists():
            raise FileReadError(f"File not found: {rel_path}")
        if real.is_dir():
            raise FileReadError(f"Path is a directory, not a file: {rel_path}")
        with real.open("rb") as handle:
            data = handle.read(self.max_file_bytes + 1)
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            raise FileReadError(f"File looks binary and was not read: {rel_path}")
        truncated = len(data) > self.max_file_bytes
        text = data[: self.max_file_bytes].decode("utf-8", errors="replace")
        return ReadResult(
            path=rel_path,
            content=text,
            truncated=truncated,
            size_bytes=real.stat().st_size,
        )
