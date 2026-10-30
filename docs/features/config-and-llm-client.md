# Config and GMI client

## What it does
- `app/config.py`: `Settings` reads configuration from environment variables and `.env`.
- `app/llm.py`: `LLMClient.complete(messages, tools=None)` calls GMI Cloud's OpenAI-compatible chat endpoint and returns an `LLMResult` (text, tool calls, token counts, latency).
- `calculate_cost(...)` returns `(I * P_in + O * P_out) / 1,000,000` as a `Decimal`.

## How to use
Copy `.env.example` to `.env` and fill in `GMI_API_KEY` and `GMI_MODEL` (exact ID from your GMI catalog). Add `PRICE_IN_PER_M` and `PRICE_OUT_PER_M` (per million tokens) from your account to get cost numbers. For `repopilot eval` with other models, add `MODEL_PRICES` (see `evals.md`).

```python
settings = Settings()
client = LLMClient(settings)
result = await client.complete([{"role": "user", "content": "hi"}])
cost = calculate_cost(result.input_tokens, result.output_tokens,
                      settings.price_in_per_m, settings.price_out_per_m)
```

## Design notes
- No model ID or price is hardcoded. A missing price makes `calculate_cost` return `None`.
- Secrets are `SecretStr`. Error messages never include the key.
- Retries: 429, 5xx, timeouts and connection errors are retried up to `llm_max_retries` (3) times with 1s, 2s, 4s backoff. 401/403 and other 4xx errors are not retried.
- The SDK's own retries are off (`max_retries=0`), so only our wrapper retries.
- If the provider omits `usage`, tokens are estimated (4 characters per token) and `usage_missing` is `True`.
- Errors: `LLMRateLimitError`, `LLMTimeoutError`, `LLMResponseError`, `LLMAuthError`, all under `LLMError`.
- Tool call arguments stay a raw JSON string. The dispatcher (Chunk 5) validates them.

## Limits
- Only chat completions. No streaming.
- The GMI base URL default is not yet verified against current GMI docs.

## How it is tested
- `tests/test_config.py`, `tests/test_cost.py`, `tests/test_llm.py` use `respx` to mock HTTP. No network.
- `tests/live/test_gmi_live.py` (marker `live`) has a tiny completion and a tool-calling probe. Run with `.venv/bin/pytest -m live -s`.
