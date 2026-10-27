# Spec: RepoPilot dashboard (Chunk 8B)

Status: **approved 2026-10-28**. Open questions resolved: contrast fixes accepted; the run form stays; the key lives in `sessionStorage`.

## Objective
A local web UI over the existing HTTP API (`app/api.py`), so one person can run RepoPilot and check its work without the terminal.

User: the project owner, on their own machine, with `repopilot serve` running.

The spec (REPOPILOT_PROMPT.md, Chunk 16) asks for exactly these six things:
1. **Issue form**: start a run (repo path or GitHub URL, issue number, optional title and body instead of fetching from GitHub).
2. **Run history**: recent runs with status, issue, model, tokens, cost, PR link.
3. **Agent timeline**: the run's events, live while it runs (SSE), stored afterwards.
4. **Diff viewer**: the patches, with added and removed lines colored.
5. **Test results**: baseline and after-patch runs, failed test names.
6. **Approve button**: approve (draft PR) or reject a `passed` run, with an explicit confirm step.

Also needed to make those work, and nothing more:
- **API key screen**: paste `REPOPILOT_API_KEY` once per browser tab.
- **Serving**: `repopilot serve` serves the built dashboard at `http://127.0.0.1:8000/ui/`.

## Assumptions (correct any of these)
1. Single user, local only, same machine as the API. No accounts, no TLS.
2. The dashboard is served by FastAPI from the same origin, so no CORS is needed. In development, Vite proxies to `127.0.0.1:8000`.
3. The API key is typed into the UI and kept in `sessionStorage` (gone when the tab closes), never in a URL, never in `localStorage`, never in the build.
4. Live events use `fetch` with a streamed body, because the browser's `EventSource` cannot send the `X-API-Key` header. Putting the key in the query string would leak it into logs and history.
5. Navigation is a hash (`#/runs/12`): the API already owns `/runs/...`, so the UI lives under `/ui/` with no router library.
6. The built files (`dashboard/dist/`) are not committed; `npm run build` makes them. If they are missing, `/ui/` returns a one-line message saying how to build.
7. Theme "Midnight Violet" from the spec, with two accessibility fixes (see Code Style): primary buttons use `#7C3AED` (white text 5.70:1; `#8B5CF6` gives only 4.23:1, below WCAG AA), and input borders use `#71717A` (the spec's `#27272A` is 1.34:1, below the 3:1 needed for form controls). `#27272A` stays for decorative dividers.

## Tech stack
| Piece | Choice | Why |
|---|---|---|
| UI | React + TypeScript (strict) | from the spec |
| Build | Vite | from the spec |
| Styling | Tailwind CSS | from the spec |
| Tests | Vitest + Testing Library (`@testing-library/react`, `user-event`, `jest-dom`) + jsdom | from the spec |
| Lint | `tsc --noEmit` | no extra linter dependency |

No router, no state library, no data-fetching library, no diff library, no component kit. Exact versions are pinned in `package.json` when installed and listed in the plan for approval. Node 20 is installed here.

Backend change (Python, small): serve `dashboard/dist` at `/ui/` with FastAPI `StaticFiles`; `/ui/` needs no key (it is static files; every data call still needs the key).

## Commands
```
cd dashboard
npm ci                      # install pinned dependencies
npm run dev                 # Vite on http://127.0.0.1:5173, proxying API calls to :8000
npm test                    # vitest run (no network)
npm run typecheck           # tsc --noEmit
npm run build               # typecheck + vite build -> dashboard/dist
cd .. && .venv/bin/repopilot serve    # dashboard at http://127.0.0.1:8000/ui/
.venv/bin/pytest            # backend tests, including /ui serving
```

## Project structure
```
dashboard/
  index.html
  package.json, package-lock.json, tsconfig.json, vite.config.ts, tailwind config
  src/
    main.tsx            entry
    App.tsx             key gate + hash routing (list | run detail)
    api.ts              typed fetch client: adds X-API-Key, maps errors to messages
    sse.ts              parses a streamed text/event-stream body into events
    types.ts            types matching app/api.py responses
    components/
      KeyGate.tsx       API key screen
      RunForm.tsx       issue form
      RunList.tsx       run history
      RunDetail.tsx     header (status, cost, PR) + tabs below
      Timeline.tsx      agent events, live
      DiffView.tsx      patches
      TestResults.tsx   test executions
      ApprovePanel.tsx  approve / reject with confirm
  tests/                *.test.tsx, one per component plus api and sse
app/api.py              + StaticFiles mount at /ui
tests/test_api.py       + /ui serving tests
docs/features/dashboard.md, docs/decisions/014-*.md
```

## Code style
TypeScript strict, function components, props typed inline, no `any`. Tailwind classes use theme tokens, not raw hex. Every async action shows loading, error and empty states. Text is plain and short.

```tsx
export function StatusBadge({ status }: { status: RunStatus }) {
  const tone = status === "passed" ? "text-success" : status === "running" ? "text-warning" : "text-error";
  return <span className={`font-medium ${tone}`}>{status}</span>; // the word, not only the color
}
```

Accessibility: real `<button>`, `<form>`, `<label>` elements; visible focus ring (`#8B5CF6`, 2px); status never shown by color alone; the live timeline is an `aria-live="polite"` region; tabs follow the WAI-ARIA tabs pattern; works with keyboard only; respects `prefers-reduced-motion`.

## Testing strategy
- **Vitest + Testing Library, tests first** for every component and for `api.ts` and `sse.ts`. `fetch` is mocked; no network.
- What the tests prove: the key goes only in the `X-API-Key` header and only to same-origin paths; a 401 returns you to the key screen; the form validates (issue ≥ 1, repo required) and shows API errors (400/409/429 with the server's `detail`); the list renders runs and empty/error states; the SSE parser handles chunks split mid-event; the timeline appends live events and stops at `end`; the diff colors `+`/`-` lines and never renders diff text as HTML; test results show failed names; **approve sends `confirm: true` only after the confirm step, the button appears only for `passed` runs without a PR, and reject sends no approve call**.
- Accessibility checks in tests: queries by role and label (if a test cannot find it by role, neither can a screen reader).
- Backend: pytest for `/ui/` (serves `index.html`, missing build message, no key needed, API routes unaffected).
- Manual check at the end: `npm run build`, `repopilot serve`, real browser at `/ui/`, read real runs, replay a stored timeline. Screenshots saved to `evidence/`.

## Boundaries
- **Always:** tests first; run `npm test`, `npm run typecheck`, `npm run build` and `pytest` before each commit; keep the key out of URLs, logs, storage other than `sessionStorage`, and the build; render all server text as text, never as HTML.
- **Ask first:** any dependency not listed above; any change to API endpoints or response shapes; starting runs that call the real model.
- **Never:** `dangerouslySetInnerHTML`; storing the key in `localStorage` or cookies; committing `node_modules` or `dist`; adding features not in the six above; loading anything from a CDN at runtime.

## Success criteria
1. With `repopilot serve` running, `http://127.0.0.1:8000/ui/` asks for the key, then shows the run history.
2. A wrong key shows "wrong key" and stays on the key screen; nothing else loads.
3. Submitting the form starts a run; its timeline shows events as they arrive and stops with the final status.
4. A finished run shows its report header, diff (colored), test results and cost.
5. For a `passed` run with no PR, approve requires a second explicit confirm click and then shows the PR link; reject records the decision; neither button shows for other runs.
6. All tests pass, typecheck is clean, the build succeeds, backend tests still pass.
7. Keyboard-only use works for every action; text contrast is at least 4.5:1 (UI parts 3:1).

## Open questions
1. **Contrast fixes:** OK to use `#7C3AED` for primary buttons and `#71717A` for input borders (assumption 7)?
2. **Starting runs from the UI:** the form calls the real model (about $0.001 per run). Keep it, or make the first version read-only?
3. **Key in `sessionStorage`:** you re-enter it per tab. OK, or would you rather it stay only in memory (re-enter on every reload)?
