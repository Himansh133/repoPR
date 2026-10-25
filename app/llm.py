"""Async client for GMI Cloud's OpenAI-compatible API.

Returns text, tool calls and token usage. Retries rate limits, server errors and
timeouts with exponential backoff. Never puts the API key in an error message.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import openai
from openai import AsyncOpenAI

from app.config import Settings

CHARS_PER_TOKEN = 4  # rough estimate, used only when the provider omits usage


class LLMError(Exception):
    """Base class for all LLM client errors."""


class LLMRateLimitError(LLMError):
    """The provider kept returning 429 after all retries."""


class LLMTimeoutError(LLMError):
    """The request kept timing out after all retries."""


class LLMResponseError(LLMError):
    """The provider returned a body we cannot read."""


class LLMAuthError(LLMError):
    """The API key was rejected (401 or 403). Not retried."""


@dataclass(frozen=True)
class ToolCall:
    """One tool call requested by the model. `arguments` is the raw JSON string."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResult:
    """Result of one model call."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    usage_missing: bool = False  # True means the token counts are our estimate
    latency_s: float = 0.0


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    price_in_per_m: Decimal | None,
    price_out_per_m: Decimal | None,
) -> Decimal | None:
    """Return `(I * P_in + O * P_out) / 1,000,000`, or None if a price is unknown."""
    if price_in_per_m is None or price_out_per_m is None:
        return None
    return (input_tokens * price_in_per_m + output_tokens * price_out_per_m) / Decimal(1_000_000)


class LLMClient:
    """Wrapper around `AsyncOpenAI` with bounded retries and clear errors."""

    def __init__(
        self,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep  # injectable so tests do not really wait
        self._client = AsyncOpenAI(
            api_key=settings.gmi_api_key.get_secret_value(),
            base_url=settings.gmi_base_url,
            timeout=settings.llm_timeout_s,
            max_retries=0,  # only our wrapper retries, so counts stay predictable
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        """Send one chat request. Retries 429, 5xx and timeouts with 1s, 2s, 4s backoff."""
        kwargs: dict[str, Any] = {
            "model": self._settings.gmi_model,
            "messages": messages,
            "max_tokens": self._settings.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        attempts = self._settings.llm_max_retries + 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                last: LLMError = LLMRateLimitError(f"Rate limited (429): {exc.message}")
            except openai.APITimeoutError:
                last = LLMTimeoutError("Request to the model timed out")
            except openai.APIConnectionError as exc:
                last = LLMError(f"Could not connect to the model API: {type(exc).__name__}")
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                raise LLMAuthError(f"API key rejected ({exc.status_code})") from None
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    last = LLMError(f"Server error ({exc.status_code}): {exc.message}")
                else:
                    raise LLMError(f"Request failed ({exc.status_code}): {exc.message}") from None
            except Exception as exc:  # SDK could not parse the body at all
                raise LLMResponseError(f"Unreadable response: {type(exc).__name__}") from None
            else:
                return self._to_result(completion, messages, time.monotonic() - started)

            if attempt < attempts - 1:
                await self._sleep(2**attempt)
        raise last

    @staticmethod
    def _to_result(completion: Any, messages: list[dict[str, Any]], latency_s: float) -> LLMResult:
        choices = getattr(completion, "choices", None)
        if not choices or getattr(choices[0], "message", None) is None:
            raise LLMResponseError("Response has no choices")
        message = choices[0].message

        text = message.content or ""
        tool_calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments)
            for c in (message.tool_calls or [])
            if getattr(c, "function", None) is not None
        ]

        usage = getattr(completion, "usage", None)
        if usage is None:
            input_tokens = len(json.dumps(messages)) // CHARS_PER_TOKEN
            output_tokens = len(text) // CHARS_PER_TOKEN
            usage_missing = True
        else:
            input_tokens = usage.prompt_tokens
            output_tokens = usage.completion_tokens
            usage_missing = False

        return LLMResult(
            text=text,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_missing=usage_missing,
            latency_s=latency_s,
        )
