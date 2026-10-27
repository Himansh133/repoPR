# Dashboard

A local web UI over the HTTP API. Spec: `docs/specs/dashboard.md`. **In progress: tasks 1-3 of 9 are built** (serving, API key screen, run history). Run detail, diff, tests, timeline, the run form and approve/reject come next.

## Use
```
cd dashboard && npm ci && npm run build      # once, and after changes
cd .. && .venv/bin/repopilot serve           # then open http://127.0.0.1:8000/ui/
cd dashboard && npm run dev                  # development: http://127.0.0.1:5173/ui/, proxies to :8000
```
Paste `REPOPILOT_API_KEY` on the first screen. It is checked against the API, kept only in this tab's `sessionStorage`, and sent only as the `X-API-Key` header.

## Design notes
- Served by FastAPI at `/ui/` from `dashboard/dist` (same origin, no CORS). The static files need no key; every data call does. Without a build, `/ui/` says how to build.
- Navigation is the URL hash (`#/`, `#/runs/12`), since the API owns `/runs/...`.
- One client (`src/api.ts`) makes every call: same-origin paths only, key header, server `detail` as the error message; any 401 clears the key and returns to the key screen.
- Server text is rendered as text, never HTML.
- Theme "Midnight Violet" as Tailwind tokens, with two contrast fixes: buttons use `#7C3AED` (white text 5.70:1) and input borders `#71717A`.
- Node 20 compatible: Vitest 4 and jsdom 27 (the newest majors need Node 22). Exact versions pinned in `package.json`.

## How it is tested
`cd dashboard && npm test` (Vitest + Testing Library, `fetch` mocked, 14 tests): the key goes only in the header, never in the URL or `localStorage`; non same-origin paths are refused; 401 clears the key and returns to the key screen; server error messages are shown; a wrong key stays on the key screen; the run list shows status words, links, PR links, empty and error states, and renders an HTML-looking title as text. `npm run build` type-checks first. Backend: `tests/test_api.py` checks `/ui/` serving, the missing-build message and that API routes still need the key.
