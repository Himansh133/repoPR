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
- Live smoke tests (completion and tool-calling probe), skipped by default.
