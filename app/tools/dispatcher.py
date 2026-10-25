"""Validates and runs tool calls requested by the model.

The model asks; the backend decides. Every failure comes back as a short message the
model can read and correct. `dispatch` never raises for bad model input. Tool output is
wrapped in <untrusted_repo_content> so repo text is clearly marked as data.
"""

import json
from dataclasses import dataclass

from pydantic import ValidationError

from app.llm import ToolCall
from app.tools.files import read_file
from app.tools.schemas import TOOL_MODELS, ReadFileArgs, SearchCodeArgs
from app.tools.search import search_code
from app.workspace import Workspace, WorkspaceError

TAG = "untrusted_repo_content"
MAX_NAME_ECHO = 50


@dataclass(frozen=True)
class ToolLimits:
    max_args_bytes: int = 4096
    max_files: int = 10  # distinct files that may be read per investigation
    max_result_chars: int = 8000


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    ok: bool
    content: str


def _wrap(tool: str, body: str, path: str = "") -> str:
    # Stop file text from closing (or reopening) our wrapper tag.
    body = body.replace(f"</{TAG}", f"<\\/{TAG}").replace(f"<{TAG}", f"<\\{TAG}")
    attr = f' path="{path}"' if path else ""
    return f'<{TAG} tool="{tool}"{attr}>\n{body}\n</{TAG}>'


class Dispatcher:
    """Runs allowed tools against one workspace and tracks which files were read."""

    def __init__(
        self,
        ws: Workspace,
        allowed_tools: list[str] | None = None,
        limits: ToolLimits | None = None,
    ) -> None:
        self.ws = ws
        self.allowed = list(allowed_tools) if allowed_tools is not None else list(TOOL_MODELS)
        self.limits = limits or ToolLimits()
        self.files_read: set[str] = set()

    def normalize(self, path: str) -> str | None:
        """Repo-relative POSIX form of a path, or None if it is not a safe path."""
        try:
            return self.ws.resolve(path).relative_to(self.ws.root).as_posix()
        except (WorkspaceError, ValueError):
            return None

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Validate and execute one tool call. Never raises for bad model input."""
        ok, content = self._run(call)
        return ToolResult(call.id, call.name, ok, content)

    def _run(self, call: ToolCall) -> tuple[bool, str]:
        if call.name not in self.allowed or call.name not in TOOL_MODELS:
            shown = str(call.name)[:MAX_NAME_ECHO]
            available = ", ".join(self.allowed)
            return False, f"Error: Unknown tool '{shown}'. Available tools: {available}"
        if len(call.arguments.encode()) > self.limits.max_args_bytes:
            return False, f"Error: Arguments too large (max {self.limits.max_args_bytes} bytes)"
        try:
            raw = json.loads(call.arguments)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            return False, "Error: Arguments must be a JSON object"
        try:
            args = TOOL_MODELS[call.name](**raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors()
            )
            return False, f"Error: Invalid arguments: {problems}"
        try:
            if isinstance(args, SearchCodeArgs):
                return True, self._search(args)
            assert isinstance(args, ReadFileArgs)
            return self._read(args)
        except WorkspaceError as exc:
            return False, f"Error: {exc}"

    def _cap(self, body: str) -> str:
        if len(body) <= self.limits.max_result_chars:
            return body
        return body[: self.limits.max_result_chars] + "\n[output truncated]"

    def _search(self, args: SearchCodeArgs) -> str:
        result = search_code(self.ws, args.query, args.path)
        if not result.matches:
            body = "No matches found."
        else:
            lines = [f"{m.path}:{m.line}: {m.text}" for m in result.matches]
            if result.truncated:
                lines.append("[more matches exist; narrow your query or path]")
            body = "\n".join(lines)
        return _wrap("search_code", self._cap(body))

    def _read(self, args: ReadFileArgs) -> tuple[bool, str]:
        norm = self.normalize(args.path)
        if norm is not None and norm not in self.files_read:
            if len(self.files_read) >= self.limits.max_files:
                return False, (
                    f"Error: File limit reached ({self.limits.max_files} distinct files). "
                    "Submit your plan using the files you have already read."
                )
        result = read_file(self.ws, args.path)
        self.files_read.add(norm or result.path)
        body = result.content
        if result.truncated:
            shown = len(result.content)
            body += f"\n[file truncated: showing first {shown} of {result.size_bytes} bytes]"
        return True, _wrap("read_file", self._cap(body), norm or result.path)
