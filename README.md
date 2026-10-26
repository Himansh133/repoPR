# RepoPilot

An AI agent that takes a GitHub issue, proposes a minimal patch, and proves it with tests in a Docker sandbox. It never creates a pull request without explicit approval.

The model never gets to say "it works". Only test results count. A run ends `passed`, `failed`, `inconclusive` or `aborted`, and the report keeps proposed changes, test results and unresolved items apart.

## How it works

```
repopilot run --repo <path-or-github-url> --issue 7
   1. copy the repo to a temp clone on a new branch (your checkout is never touched)
   2. run the tests in a Docker sandbox (baseline)
   3. the model searches and reads the code, then submits a plan that cites files it really read
   4. the model proposes a unified diff; the backend validates and applies it
   5. the backend runs the tests in the sandbox; on failure the model gets the failed test names
      and bounded output, and may repair (max 2)
   6. report: plan, diff, test results, tokens, cost, honest final status
```
The backend owns the workflow. The model can request `search_code`, `read_file` and `propose_patch`; it cannot run tests or commands.

## Setup
```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .
./docker/build.sh                 # builds the sandbox image once (needs Docker)
cp .env.example .env              # then fill it in (see docs/live-testing.md)
.venv/bin/pytest                  # no network, no model, no Docker needed
```
Optional: install `ripgrep` (`sudo apt install ripgrep`). Without it, search falls back to pure Python.

## Use
```
repopilot run --repo . --issue 7                      # local git repo, issue fetched from GitHub
repopilot run --repo https://github.com/o/r --issue 7
repopilot run --repo . --issue 1 --issue-file issue.json    # no GitHub needed
repopilot runs | show <id> | usage [--today]
repopilot eval --models a,b --issues evals/benchmark_issues.jsonl
repopilot compare evals/results/<run>
```
Exit codes for `run`: 0 passed, 1 failed, 3 inconclusive, 4 aborted, 5 usage or budget error.

## Safety
Repo, issue and test-output text is untrusted and never followed as instructions. No model-written commands. Tests run in a locked-down container (no network, non-root, read-only, resource limits). Paths cannot escape the repo. Budgets cap steps, tokens, time, repairs, daily runs and daily spend. See [docs/security.md](docs/security.md).

## Limitations
- Python repos tested with pytest only; the sandbox image has no other packages, so tests that need third-party dependencies will fail.
- Nothing has been run against a real model yet; see [docs/live-testing.md](docs/live-testing.md).
- Pull request creation, the API and the dashboard are not built yet.
- The bundled benchmark has three easy synthetic issues. Real comparisons need real issues.

## Project status

| Chunk | Name | Status | Tests |
|---|---|---|---|
| 0 | Project scaffold | Done | 2 |
| 1 | Config and GMI client | Mocked tests done, live smoke test pending | 22 (+2 live) |
| 2 (A) | Workspace and path safety | Done | 63 total |
| 2 (B) | GitHub issue fetching | Done | 84 total |
| 2 (C) | search_code and read_file tools | Done | 134 total (109 without ripgrep installed) |
| 3 (A) | Tool schemas and dispatcher | Done | 197 total (144 without ripgrep installed) |
| 3 (B) | State machine and investigation loop | Done | 243 total (171 without ripgrep installed) |
| 4 (A) | Patch validation and working copy | Done | 295 total |
| 4 (B) | Docker sandbox | Done | 326 total (+9 Docker) |
| 4 (C) | Test run, repair loop and report | Done | 366 total (+11 Docker) |
| 5 (A) | SQLite persistence and usage tracking | Done | 390 total |
| 5 (B) | `repopilot` CLI | Done | 426 total |
| 5 (C) | Evaluation harness | Done | 470 total (+11 Docker, +2 live); 398 without ripgrep installed |
| 6 | Model comparison report | Waiting for a live GMI run | - |
| 7 | Draft PR with approval gate | Not started | - |
| 8 | FastAPI layer, dashboard, release polish | Not started | - |

## Docs
`docs/features/` (one page per feature), `docs/decisions/` (why), `docs/security.md`, `docs/live-testing.md`, `CHANGELOG.md`.

License: MIT.
