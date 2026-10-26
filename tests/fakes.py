"""Test helpers: a scripted fake LLM. No network, no real model."""

import copy
import difflib
import json
from pathlib import Path

from app.llm import LLMResult, ToolCall
from app.sandbox import ExecStatus, Execution


class FakeLLM:
    """Replays a scripted list of LLMResult objects (or exceptions to raise).

    Records a snapshot of every request so tests can inspect what the model was sent.
    Running out of script raises, so a runaway loop fails the test instead of hanging.
    """

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if not self._script:
            raise RuntimeError("FakeLLM script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name, args, id="c1"):
    raw = args if isinstance(args, str) else json.dumps(args)
    return ToolCall(id=id, name=name, arguments=raw)


def reply(*calls, text="", in_tokens=100, out_tokens=50):
    return LLMResult(
        text=text,
        tool_calls=list(calls),
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


def plan_args(files=("shop/cart.py",), summary="Guard against empty lists.", steps=None):
    return {
        "summary": summary,
        "cited_files": [{"path": p, "reason": "relevant"} for p in files],
        "steps": steps or ["Return 0 when the cart is empty."],
    }


# --- sandbox and patch helpers ---------------------------------------------------------


def make_diff(path, old, new, new_file=False):
    """A unified diff in the form the model is asked to produce."""
    a = "/dev/null" if new_file else f"a/{path}"
    return "".join(
        difflib.unified_diff(
            [] if new_file else old.splitlines(True), new.splitlines(True), a, f"b/{path}"
        )
    )


def execution(status, failed=(), log="", reason=None, exit_code=None, id="e"):
    codes = {ExecStatus.PASSED: 0, ExecStatus.FAILED: 1, ExecStatus.INCONCLUSIVE: 5}
    return Execution(
        id=id,
        profile="python-pytest",
        status=status,
        exit_code=codes.get(status) if exit_code is None else exit_code,
        duration_s=0.5,
        failed_tests=list(failed),
        log_tail=log,
        truncated=False,
        reason=reason,
    )


class FakeSandbox:
    """Returns scripted Executions and records what was in the work dir at call time."""

    def __init__(self, *script, on_call=None):
        self._script = list(script)
        self.calls: list[dict] = []
        self._on_call = on_call

    def run_tests(self, profile, workdir):
        cart = Path(workdir) / "shop" / "cart.py"
        self.calls.append({"profile": profile, "workdir": Path(workdir), "cart": cart.read_text()})
        if self._on_call:
            self._on_call(len(self.calls))
        if not self._script:
            raise RuntimeError("FakeSandbox script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
