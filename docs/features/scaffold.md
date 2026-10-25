# Project scaffold

## What it does
Sets up the folder layout, tooling and config so later chunks have a place to go.

## How to use
```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/ruff check
```

## Design notes
- `pytest` skips `live` and `docker` tests by default. Run them with `-m live` or `-m docker`.
- `pythonpath = ["."]` lets tests import `app` without installing the package.
- Ruff checks errors, imports, bugbear and pyupgrade rules at line length 100.

## Limits
Only a smoke test exists. No real features yet.

## How it is tested
`tests/test_smoke.py` checks that `app` imports and that the two markers are registered.
