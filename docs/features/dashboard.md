# Dashboard

A local web UI over the HTTP API. Spec: `docs/specs/dashboard.md`. All six spec features are built: issue form, run history, live agent timeline, diff viewer, test results, approve/reject.

## Use
```
cd dashboard && npm ci && npm run build      # once, and after changes
cd .. && .venv/bin/repopilot serve           # then open http://127.0.0.1:8000/ui/
cd dashboard && npm run dev                  # development: http://127.0.0.1:5173/ui/, proxies to :8000
```
Paste `REPOPILOT_API_KEY` on the first screen. It is checked against the API, kept only in this tab's `sessionStorage`, and sent only as the `X-API-Key` header.

## Views
- **Home (`#/`)**: the **New run** form (repo path or GitHub URL, issue number, optional title and body to skip GitHub, "allow unpriced" off by default; it calls the real model) and the **run history** table.
- **Run (`#/runs/<id>`)**: header (issue, repo, model, status as a word, reason, repairs, tokens, cost), the **Decision** panel, and four tabs (arrow keys move between them):
  - **Timeline**: every agent event, streamed live while the run is going and replayed afterwards; ends with "Finished: <status>" and reloads the header.
  - **Diff**: each patch with its files and counts; `+` lines green, `-` red, `@@` violet; the `+`/`-` stays visible.
  - **Tests**: before the patch and after each patch, with status, time and failed test names.
  - **Report**: the stored text report.
- **Decision panel**: only for `passed` runs without a PR. **Approve…** opens a confirm step (optional note, "close the issue when merged" off by default); only **Create draft PR** sends `{confirm: true}`. **Reject** sends only `/reject`. Server errors are shown. Once a PR exists, the panel shows its link instead.

## Design notes
- Served by FastAPI at `/ui/` from `dashboard/dist` (same origin, no CORS). The static files need no key; every data call does. Without a build, `/ui/` says how to build.
- Navigation is the URL hash (`#/`, `#/runs/12`), since the API owns `/runs/...`.
- One client (`src/api.ts`) makes every call: same-origin paths only, key header, server `detail` as the error message; any 401 clears the key and returns to the key screen.
- Server text is rendered as text, never HTML.
- Theme "Midnight Violet" as Tailwind tokens, with three contrast fixes: buttons use `#7C3AED` (white text 5.70:1), input borders `#71717A`, and violet text `#A78BFA` (6.51:1 on panels; `#8B5CF6` was 4.18:1).
- Node 20 compatible: Vitest 4 and jsdom 27 (the newest majors need Node 22). Exact versions pinned in `package.json`.

## Limits
- The API does not return approve/reject decisions, so after a page reload a rejected run shows the buttons again; approving it then fails with the server's "was rejected" message.
- A run opens from its `#N` link (click or Enter); clicking elsewhere in the row does nothing.

## Real-browser pass (2026-10-29)
Headless Chrome against `repopilot serve` and the real database (12 runs, two with live draft PRs). Nothing was started, approved or rejected.
- Key screen: a wrong key shows "Wrong key"; the right key is kept in `sessionStorage` only, `localStorage` stays empty, every API request carries the header and no URL contains the key.
- Home: form and run history (12 rows, 2 PR links); an empty submit sends nothing.
- Run #9 (passed, PR): PR link, replayed timeline ending "Finished: passed", diff, tests (baseline failed with the test name, patch 1 passed), report. Arrow keys move between tabs.
- Run #8 (passed, no PR): Decision panel; **Approve…** opens the confirm step with "close the issue" unchecked; Cancel.
- Runs #4 (aborted) and #11 (error, no events): status and reason shown; empty tabs say "No patch was applied.", "No tests were run.", "No report yet."; `#/runs/999` says "No run with id 999".
- Keyboard: Tab order is form fields, Start run, then each run link and PR link; focus ring visible.
- 390 px wide: no horizontal scroll.
- Contrast (text on background): fg 19.06 and 16.97, muted 7.76 and 6.91, link 7.83 and 6.97, success 7.84, violet 6.51; button white on `#7C3AED` 5.70.
- Console: only the expected 401 (wrong key) and 404s (run 999).
- Screenshots (local, `evidence/` is git-ignored): `5_dashboard_key_screen.png` to `14_dashboard_mobile.png`.

## How it is tested
`cd dashboard && npm test` (Vitest + Testing Library, `fetch` mocked, 31 tests): the key goes only in the header, never in the URL or `localStorage`; non same-origin paths are refused; 401 clears the key and returns to the key screen; server error messages are shown; a wrong key stays on the key screen; the run list shows status words, links, PR links, empty and error states, and renders an HTML-looking title as text; the SSE parser handles events split across chunks and CRLF; the timeline streams to `end` and reloads a run whose status changed; tabs move with arrow keys; test results show failed names; diff lines are colored and a `<script>` line stays text; approve needs a second click and sends `confirm: true` with the note and `closes_issue`, cancel sends nothing, reject calls only `/reject`, server errors are shown, the panel is hidden for non-passed runs and shows the PR link once it exists; the form posts the right body, leaves out title and body for GitHub issues, validates before sending and shows the server's reason (409). `npm run build` type-checks first. Backend: `tests/test_api.py` checks `/ui/` serving, the missing-build message and that API routes still need the key.
