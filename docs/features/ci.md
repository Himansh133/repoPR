# Continuous integration

## What it does
`.github/workflows/ci.yml` runs the default checks on every push to `main` and every pull request.

| Job | Steps |
|---|---|
| `python` (3.11 and 3.12) | install ripgrep, `pip install -r requirements.lock && pip install --no-deps -e .`, `ruff check`, `pytest -q` |
| `dashboard` (Node 20) | `npm ci`, `npm test`, `npm run typecheck`, `npm run build` in `dashboard/` |

## Design notes
- No secrets. The default tests never call the network, a model or Docker, so CI needs none. `permissions: contents: read`.
- ripgrep is installed so the ripgrep search tests run instead of being skipped.
- Docker (`-m docker`) and live (`-m live`) tests are not run in CI; run them locally.
- A newer push to the same branch cancels the older run.
- CI installs the exact versions in `requirements.lock`, so a new upstream release cannot change a run. `requirements*.txt` keep the ranges; regenerate the lock (steps in its header) to move forward.

## How it is tested
Checked by hand before adding: a clean clone with no `.env`, a fresh Python 3.12 venv and a fresh `npm ci` ran every step. That found a lint error a stale local `.ruff_cache` had hidden (fixed in the same branch).
