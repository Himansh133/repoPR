"""Tests for app.llm.LLMClient. All HTTP is mocked with respx."""

import httpx
import pytest
import respx

from app.config import Settings
from app.llm import (
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)

BASE = "https://gmi.test/v1"
URL = f"{BASE}/chat/completions"
MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def settings(monkeypatch):
    for name in ("GITHUB_TOKEN", "PRICE_IN_PER_M", "PRICE_OUT_PER_M"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GMI_API_KEY", "sk-test")
    monkeypatch.setenv("GMI_MODEL", "test-model")
    monkeypatch.setenv("GMI_BASE_URL", BASE)
    return Settings(_env_file=None)


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(settings, sleeps):
    async def fake_sleep(seconds):
        sleeps.append(seconds)

    return LLMClient(settings, sleep=fake_sleep)


def ok_body(content="hello", usage=True, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    return body


@respx.mock
async def test_success_returns_text_and_usage(client):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ok_body("hello")))
    result = await client.complete(MESSAGES)
    assert result.text == "hello"
    assert result.input_tokens == 12
    assert result.output_tokens == 5
    assert result.usage_missing is False
    assert result.latency_s >= 0
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert b'"max_tokens":1200' in sent.content.replace(b" ", b"")
    assert b"test-model" in sent.content


@respx.mock
async def test_tool_call_is_parsed(client):
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        }
    ]
    respx.post(URL).mock(
        return_value=httpx.Response(200, json=ok_body(content=None, tool_calls=calls))
    )
    result = await client.complete(MESSAGES, tools=[{"type": "function", "function": {}}])
    assert result.text == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == '{"path": "a.py"}'


@respx.mock
async def test_missing_usage_is_estimated(client):
    respx.post(URL).mock(return_value=httpx.Response(200, json=ok_body("abcdefgh", usage=False)))
    result = await client.complete(MESSAGES)
    assert result.usage_missing is True
    assert result.input_tokens > 0
    assert result.output_tokens == 2  # 8 chars // 4


@respx.mock
async def test_429_then_success_retries_with_backoff(client, sleeps):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=ok_body("finally")),
        ]
    )
    result = await client.complete(MESSAGES)
    assert result.text == "finally"
    assert route.call_count == 3
    assert sleeps == [1, 2]


@respx.mock
async def test_429_every_time_raises_rate_limit(client, sleeps):
    route = respx.post(URL).mock(
        return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
    )
    with pytest.raises(LLMRateLimitError):
        await client.complete(MESSAGES)
    assert route.call_count == 4  # 1 try + 3 retries
    assert sleeps == [1, 2, 4]


@respx.mock
async def test_5xx_is_retried(client):
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503, json={"error": {"message": "down"}}),
                     httpx.Response(200, json=ok_body("up"))]
    )
    assert (await client.complete(MESSAGES)).text == "up"
    assert route.call_count == 2


@respx.mock
async def test_timeout_raises_timeout_error(client):
    route = respx.post(URL).mock(side_effect=httpx.ConnectTimeout("too slow"))
    with pytest.raises(LLMTimeoutError):
        await client.complete(MESSAGES)
    assert route.call_count == 4


@respx.mock
async def test_malformed_body_raises_response_error(client):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(LLMResponseError):
        await client.complete(MESSAGES)


@respx.mock
async def test_non_json_body_raises_response_error(client):
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(LLMResponseError):
        await client.complete(MESSAGES)


@respx.mock
async def test_401_is_not_retried(client):
    route = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(LLMAuthError):
        await client.complete(MESSAGES)
    assert route.call_count == 1


@respx.mock
async def test_other_4xx_raises_llm_error_without_retry(client):
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    with pytest.raises(LLMError):
        await client.complete(MESSAGES)
    assert route.call_count == 1


@respx.mock
async def test_api_key_never_in_error_message(client):
    respx.post(URL).mock(return_value=httpx.Response(401, json={"error": {"message": "bad"}}))
    with pytest.raises(LLMAuthError) as exc:
        await client.complete(MESSAGES)
    assert "sk-test" not in str(exc.value)
