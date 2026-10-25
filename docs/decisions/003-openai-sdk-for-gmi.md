# 003: OpenAI SDK against GMI, with our own retry wrapper

**Decision:** Use the OpenAI Python SDK pointed at GMI Cloud's OpenAI-compatible endpoint. Turn off the SDK's retries and retry in our own wrapper. Pin `openai==2.54.0`.

**Why:** GMI is OpenAI-compatible, so we avoid writing an HTTP client. Our own retry loop gives exact retry counts and backoff that tests can check.

**Pin:** `openai` 3.x uses a separate HTTP library (`httpx2`), which `respx` cannot mock. 2.54.0 uses `httpx`, so HTTP mocks work. Revisit when `respx` supports `httpx2`, or switch tests to a mock transport.
