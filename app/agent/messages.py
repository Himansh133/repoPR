"""Helpers for building chat messages."""

from app.llm import LLMResult


def assistant_message(result: LLMResult) -> dict:
    """The assistant turn (text and tool calls) to append before the tool results."""
    return {
        "role": "assistant",
        "content": result.text or None,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in result.tool_calls
        ],
    }
