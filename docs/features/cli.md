# The `repopilot` CLI

## Install
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e .
./docker/build.sh                      # once: builds the sandbox image
cp .env.example .env                   # then fill it in
```

## Commands
```
repopilot run --repo <local-git-repo | https://github.com/o/r> --issue 7 [options]
repopilot runs [--limit N]             # recent runs
repopilot show <run-id>                # the stored report
repopilot usage [--today]              # token and cost totals
```
`run` options: `--github owner/repo` (if it cannot be worked out), `--issue-file issue.json` (`{"title": "...", "body": "...", "labels": []}`, skips GitHub), `--allow-unpriced`, `--cleanup` (delete the working copy afterwards).

## What `run` does
1. Loads settings. A missing or invalid setting prints one line naming it (never a value).
2. Checks the daily limits (run count, spend cap, prices set). A blocked run stops here, before any clone, GitHub call or model call.
3. Resolves the repo: a local git repo, or an `https://github.com/o/r` URL (cloned to a temp folder that is removed afterwards).
4. Gets the issue from GitHub (owner/repo from `--github`, the URL, or the local repo's `origin`), or from `--issue-file`.
5. Records the run, runs the full agent (baseline tests, investigate, patch, sandbox tests, up to 2 repairs), saves everything to SQLite, prints the report, the run id, the cost and the working copy path.

## Exit codes
| Code | Meaning |
|---|---|
| 0 | passed |
| 1 | failed |
| 2 | bad arguments (argparse) |
| 3 | inconclusive |
| 4 | aborted, or the run crashed |
| 5 | usage, configuration or budget error |
| 130 | interrupted |

## Design notes
- `main(argv, deps=..., out=..., err=...)`: the LLM, sandbox, GitHub client, clone function and settings are injected through `Deps`, so tests run the whole CLI with fakes.
- `runs`, `show` and `usage` need no GMI key; they read only `DB_PATH`.
- A crash is recorded as status `error` in the database (it still counts toward the daily run limit) and printed as one line, not a traceback.
- Secrets are never printed. Tested.
- The working copy is kept by default so a later step can open a pull request from its branch.

## How it is tested
`tests/test_cli.py` (36 tests): a full run with saved database rows and cost, every exit code, `--cleanup`, a crash, a sandbox error, issue fetching and `origin` detection, GitHub errors, bad repo paths, URL clone and cleanup, non-GitHub URLs, missing configuration, run limit / spend cap / unpriced blocks (and that no model call is made), secrets never printed, `runs`, `show`, `usage`, and bad arguments.
