# HTTP API

The same runs and approvals as the CLI, over HTTP. It is what the dashboard will use.

## Start
```
# .env: REPOPILOT_API_KEY=<at least 16 characters>, e.g. python3 -c "import secrets; print(secrets.token_urlsafe(24))"
repopilot serve                    # http://127.0.0.1:8000, this machine only
repopilot serve --port 9000
repopilot serve --host 0.0.0.0     # reachable from the network: prints a warning
```
Interactive docs: `http://127.0.0.1:8000/docs`. Every endpoint except `/health` needs the header `X-API-Key: <REPOPILOT_API_KEY>`.

## Endpoints
| Method and path | What it does |
|---|---|
| `GET /health` | `status`, `version`, `gmi_configured`, `docker`. No key needed, no secrets. |
| `POST /runs` | Start a run. Body: `repo` (local git repo or `https://github.com/o/r`), `issue`, optional `github`, `issue_title` + `issue_body` (skip GitHub), `allow_unpriced`. **202** with `{id, status: "running", events}`. |
| `GET /runs?limit=20` | Recent runs, without reports. |
| `GET /runs/{id}` | One run with its report, `pr_url` and `decision` (the latest approve/reject: `decision`, `decided_at`, `note`; `null` if none). |
| `GET /runs/{id}/events` | Server-sent events: each agent event as `event: <kind>` / `data: <json>`, live while the run is going, then `event: end` with the final status. A finished run's stored events are replayed. |
| `GET /runs/{id}/diff` | The patches (files, line counts, diff). |
| `GET /runs/{id}/tests` | Baseline and after-patch test results. |
| `GET /runs/{id}/cost` | Total, estimate flag, tokens, and every LLM call with its cost. |
| `POST /runs/{id}/approve` | Body `{confirm: true, closes_issue?, note?}`. Same checks as `repopilot approve`, then opens the draft PR. Returns `{id, pr_url}`. |
| `POST /runs/{id}/reject` | Body `{note?}`. Records the rejection; nothing is sent to GitHub. |
| `POST /runs/{id}/cancel` | 202 `{id, cancelling: true}`: the running run stops at its next step and ends `aborted` with reason `cancelled`. 409 if that run is not the one running, 404 if it does not exist. |

```
curl -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"repo": "/home/me/myrepo", "issue": 3}' http://127.0.0.1:8000/runs
curl -N -H "X-API-Key: $KEY" http://127.0.0.1:8000/runs/12/events
```

## Status codes
| Code | When |
|---|---|
| 200 / 202 | OK / run started |
| 400 | A problem you can fix: bad repo, bad config, `confirm` not true |
| 401 | Missing or wrong `X-API-Key` |
| 404 | Unknown run |
| 409 | A run is already in progress; the run was rejected, already has a PR, is not `passed`, or is still running |
| 422 | The body does not match the schema (wrong types, unknown fields, too long) |
| 429 | Rate limit (with `Retry-After`) or the daily run / spend cap |
| 502 | Approved, but GitHub failed to create the PR |

Errors are `{"detail": "<one line>"}`.

## Design notes
- `app/service.py` holds the run and approval logic; the CLI and the API both call it, so the checks exist once. The CLI tests guard the shared code.
- Runs execute one at a time in a worker thread with its own event loop (the Docker sandbox call blocks). A second `POST /runs` gets 409. See decision 013.
- Events reach the stream through an `EventLog` listener; a listener error is logged and never breaks the run.
- At startup, runs left `running` for over an hour (by a process that stopped) are marked `error`. Newer ones are left alone, since a CLI run may still own them.
- Rate limits (in memory): `POST /runs` 5 a minute; approve and reject 10 a minute. The daily run limit and spend cap still apply.
- The API key is compared in constant time. The server refuses to start without one, or with one under 16 characters.
- Responses leave out the working-copy path and never contain the GMI key, GitHub tokens or the API key. Tested.

## Limits
- One user, one key, one run at a time, state in one process. No accounts or TLS: keep it on 127.0.0.1, or put it behind a proxy you trust.
- A local `repo` path is a path on the server's machine.

## How it is tested
`tests/test_api.py` (59 tests, TestClient; runs execute for real in the worker thread with the fake LLM, sandbox and PR writer): startup without or with a short key; `/health`; every protected endpoint without, with a wrong and with an empty key; a full run read back through every endpoint; 422 bodies; 400 bad repos that free the run slot; 409 while a run is going (held mid-run); 429 daily limit and rate limit with `Retry-After`; a crash recorded and the slot freed; 404s; no secrets in any response; SSE replay and live streaming while the run is held; approve needs `confirm: true`, one PR per run, reject blocks approve, a running run cannot be approved; the run response carries the latest decision; cancel stops a run held at its baseline (ends `aborted`, `cancelled`), and refuses finished (409), missing (404) and keyless (401) requests; stale runs marked at startup. A mutation check (auth disabled) makes all 27 auth tests fail.
