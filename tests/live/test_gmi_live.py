"""Live smoke tests. They call the real GMI Cloud API and may cost money.

Run only on request:  .venv/bin/pytest -m live -s
Needs a real .env with GMI_API_KEY and GMI_MODEL (and price values for cost output).
"""

import json

import pytest

from app.config import Settings
from app.llm import LLMClient, calculate_cost

pytestmark = pytest.mark.live


@pytest.fixture
def client():
    return LLMClient(Settings())


async def test_tiny_completion(client):
    result = await client.complete(
        [{"role": "user", "content": "Reply with the single word: pong"}]
    )
    print(f"\ntext={result.text!r} in={result.input_tokens} out={result.output_tokens} "
          f"usage_missing={result.usage_missing} latency={result.latency_s:.2f}s")
    assert result.text.strip()


async def test_tool_calling_probe(client):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    result = await client.complete(
        [{"role": "user", "content": "What is the weather in Paris? Use the tool."}],
        tools=tools,
    )
    settings = Settings()
    cost = calculate_cost(
        result.input_tokens, result.output_tokens,
        settings.price_in_per_m, settings.price_out_per_m,
    )
    print(f"\ntool_calls={[(c.name, c.arguments) for c in result.tool_calls]} cost={cost}")
    assert result.tool_calls, "Model did not return a tool call: tool calling may be unsupported."
    assert result.tool_calls[0].name == "get_weather"
    json.loads(result.tool_calls[0].arguments)
