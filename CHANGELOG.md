# Changelog

## Unreleased

### Added
- Project scaffold: package layout, pytest (with `live` and `docker` markers), ruff, env template.
- `app/config.py`: settings from env/`.env`, secrets as `SecretStr`, prices from config.
- `app/llm.py`: async GMI client with bounded retries, usage tracking and typed errors; cost calculator.
- `app/workspace.py`: safe repo root, path resolver, bounded file reader, GitHub clone with URL allowlist.
- `app/github.py`: read-only GitHub client for issues, comments and repo info, with size caps and typed errors.
- `app/tools/`: `search_code` (literal, bounded, ripgrep with Python fallback) and `read_file`; fixture repo with a bug and prompt-injection text.
- `app/tools/schemas.py` and `dispatcher.py`: validated tool calls with an allowlist, argument and result limits, a 10-file read limit, and untrusted-content wrapping.
- `app/agent/`: state machine, in-memory event log, prompts, and the read-only investigation loop with step, token, time and invalid-call budgets; evidence-backed plans via `submit_plan`.
- `tests/fakes.py`: scripted fake LLM.
- `app/patch.py`: strict unified-diff parser, temp clone on a new branch, `git apply --check` then apply and commit, with hooks off.
- `app/sandbox.py`, `docker/`: Docker sandbox with fixed test profiles, no network, non-root, read-only, resource limits, bounded logs; `docker` integration tests.
- `app/agent/fix.py`: full run with baseline tests, patch, sandbox tests, up to 2 repairs, and honest final status (`passed`, `failed`, `inconclusive`, `aborted`).
- `app/report.py`: plain-text report separating changes, test results and unresolved items.
- `app/db.py`: SQLite persistence for runs, events, patches, test results, usage and approvals; exact cost tracking; daily run and spend limits.
- `app/paths.py`: shared cache directory helper.
- `app/cli.py`: `repopilot run | runs | show | usage`, exit codes per final status, daily-limit checks, `--issue-file`, `--cleanup`; console script.
- `docs/live-testing.md`: steps for the first real GMI run.
- `app/evals.py`, `evals/`: benchmark loader with validation, metrics (resolution, patch test pass, tool-call success, repair success, cost, latency), comparison table, runner with saved artifacts; three seed repos and issues.
- `repopilot eval` and `repopilot compare`.
- Live smoke tests (completion and tool-calling probe), skipped by default.
- `app/pr.py` and `repopilot approve` / `reject`: draft PR only after explicit approval, built from the tested working copy through the GitHub API with a separate write token; `Refs #N` by default, `--closes-issue` for `Fixes #N`.
- `app/db.py`: schema version 2 with `runs.pr_url` and in-place migration from version 1.
- `app/api.py` and `repopilot serve`: FastAPI layer with API-key auth, background runs, live SSE events, diff/tests/cost endpoints, approve/reject, rate limits.
- `app/service.py`: run and approval logic shared by the CLI and the API.
- `EventLog` listener (`on_event`) for live event streaming; `Database.fail_stale_runs`.
- `dashboard/`: React + Vite + Tailwind UI served at `/ui/` with API key screen, issue form, run history, live timeline, diff viewer, test results and approve/reject panel; spec in `docs/specs/dashboard.md`.
- `docs/demo.md`: demo script; `docs/images/`: dashboard screenshots and a demo GIF captured from stored runs.
- Cancel a running run: `POST /runs/{id}/cancel`, a `should_stop` check at every step of the agent, and a **Cancel run** button in the dashboard. The run ends `aborted` with the reason `cancelled`.
- Python sandbox: a `requirements.txt` is installed once, wheels only, into a folder outside the repo that the offline tests see read-only on `PYTHONPATH`; options, paths and URLs in the file are refused; `Sandbox.release` deletes the folder when the run ends.
- `.github/workflows/ci.yml`: `ruff check` and `pytest` on Python 3.11 and 3.12 (with ripgrep), and the dashboard's `npm test`, typecheck and build on Node 20, for every push to `main` and every PR. No secrets; Docker and live tests are not run.

### Changed
- `README.md`: architecture diagram, screenshots and demo GIF, benchmark results, live draft PRs, current limitations and status.

### Fixed
- `GET /runs/{id}` returns the latest approve/reject `decision`, and the dashboard shows a stored rejection after a reload instead of offering the buttons again.
- `app/patch.py`: hunk header counts are recounted from the hunk body before validation, so a correct patch with miscounted headers is no longer rejected.
- `repopilot runs`: long repo and model names are shortened so columns no longer run together.
- Eval table and `summary.json`: cost per issue is rounded to 8 decimal places.
- Dashboard: violet text (timeline event kinds, diff `@@` lines) now uses `#a78bfa` (6.51:1 on panels) instead of `#8b5cf6` (4.18:1, below WCAG AA).
