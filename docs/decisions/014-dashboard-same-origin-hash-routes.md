# 014: Dashboard served by the API at /ui/, with hash routes and fetch-streamed events

**Decision:** FastAPI serves the built dashboard from `dashboard/dist` at `/ui/`. Navigation uses the URL hash. Live events are read with `fetch` and a streamed body, not `EventSource`. The API key is kept in `sessionStorage` and sent only as the `X-API-Key` header.

**Why:** Same origin means no CORS setup. The API already owns `/runs/...`, so hash routes avoid clashes and need no router library. `EventSource` cannot send headers, and a key in the query string would end up in logs and browser history.

**Cost:** The dashboard has to be built (`npm run build`) before `/ui/` works. The key must be pasted once per tab. A small hand-written SSE parser has to be maintained (tested on split chunks).
