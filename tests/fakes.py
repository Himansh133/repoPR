"""Test helpers: a scripted fake LLM. No network, no real model."""

import copy
import json

from app.llm import LLMResult, ToolCall


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
