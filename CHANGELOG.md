# Changelog

## Unreleased

### Added
- Project scaffold: package layout, pytest (with `live` and `docker` markers), ruff, env template.
- `app/config.py`: settings from env/`.env`, secrets as `SecretStr`, prices from config.
- `app/llm.py`: async GMI client with bounded retries, usage tracking and typed errors; cost calculator.
- `app/workspace.py`: safe repo root, path resolver, bounded file reader, GitHub clone with URL allowlist.
- Live smoke tests (completion and tool-calling probe), skipped by default.
