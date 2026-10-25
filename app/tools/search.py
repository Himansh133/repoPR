"""The `search_code` tool: a bounded, literal search inside the workspace.

Uses ripgrep when it is installed, otherwise a pure-Python fallback with the same
rules. ripgrep is run with a fixed argument list (no shell); the query goes after
`-e`, so a query like `--files` is just text. Search is literal (not regex).
Neither path follows symlinks, so a link cannot lead outside the repo.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.workspace import BINARY_SNIFF_BYTES, Workspace, WorkspaceError

MAX_QUERY_CHARS = 200
RG_TIMEOUT_S = 10
DEFAULT_MAX_MATCHES = 20
DEFAULT_MAX_SNIPPET_CHARS = 200
MAX_MATCHES_PER_FILE = 5
MAX_SEARCH_FILE_BYTES = 1_000_000
SKIP_DIRS = {"node_modules", "__pycache__"}


@dataclass(frozen=True)
class Match:
    path: str  # repo-relative, forward slashes
    line: int
    text: str  # one line, capped


@dataclass(frozen=True)
class SearchResult:
    matches: list[Match] = field(default_factory=list)
    truncated: bool = False  # True if more matches existed than were returned


def search_code(
    ws: Workspace,
    query: str,
    path: str | None = None,
    max_matches: int = DEFAULT_MAX_MATCHES,
    max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
) -> SearchResult:
    """Search for a literal string. Optionally limit to a repo-relative file or folder."""
    if not isinstance(query, str) or not query.strip():
        raise WorkspaceError("Search query must be a non-empty string")
    if "\x00" in query:
        raise WorkspaceError("Search query contains a null byte")
    if len(query) > MAX_QUERY_CHARS:
        raise WorkspaceError(f"Search query is too long (max {MAX_QUERY_CHARS} characters)")

    target = ws.root
    if path is not None:
        target = ws.resolve(path)  # raises PathSafetyError
        if not target.exists():
            raise WorkspaceError(f"Search path not found: {path}")

    limit = max_matches + 1  # one extra tells us whether results were cut off
    if shutil.which("rg"):
        found = _search_rg(ws.root, target, query, limit)
    else:
        found = _search_python(ws.root, target, query, limit)

    truncated = len(found) > max_matches
    matches = [
        Match(m.path, m.line, _cap(m.text, max_snippet_chars)) for m in found[:max_matches]
    ]
    return SearchResult(matches=matches, truncated=truncated)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _search_rg(root: Path, target: Path, query: str, limit: int) -> list[Match]:
    cmd = [
        "rg", "--json", "--fixed-strings", "--no-config", "--sort", "path",
        "--max-count", str(MAX_MATCHES_PER_FILE),
        "--max-filesize", "1M",
        "--glob", "!.git",
        "-e", query,
        "--", os.path.relpath(target, root),  # always a path, or rg would read stdin
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError("Search timed out") from None

    found: list[Match] = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        rel = os.path.normpath(data["path"]["text"]).replace(os.sep, "/")
        found.append(Match(rel, data["line_number"], data["lines"].get("text", "").rstrip("\r\n")))
        if len(found) >= limit:
            break
    if proc.returncode == 2 and not found:
        raise WorkspaceError("Search failed")
    return found


def _search_python(root: Path, target: Path, query: str, limit: int) -> list[Match]:
    found: list[Match] = []
    for file in _iter_files(target):
        try:
            if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            data = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            continue
        rel = file.relative_to(root).as_posix()
        hits = 0
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if query in line:
                found.append(Match(rel, number, line))
                hits += 1
                if len(found) >= limit:
                    return found
                if hits >= MAX_MATCHES_PER_FILE:
                    break
    return found


def _iter_files(target: Path):
    """Yield regular files under `target` in sorted order. Skips hidden dirs and symlinks."""
    if target.is_file():
        if not target.is_symlink():
            yield target
        return
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(filenames):
            file = Path(dirpath) / name
            if not name.startswith(".") and not file.is_symlink():
                yield file
