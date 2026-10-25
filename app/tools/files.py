"""The `read_file` tool: a thin wrapper over the workspace's bounded, safe reader."""

from app.workspace import ReadResult, Workspace


def read_file(ws: Workspace, path: str) -> ReadResult:
    """Read a repo-relative text file (bounded). Raises WorkspaceError subclasses."""
    return ws.read_file(path)
