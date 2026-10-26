# First live run (you run this)

Default tests never call a real model. This is how to try the real thing.

1. `cp .env.example .env` and fill in `GMI_API_KEY`, `GMI_MODEL` (exact ID from your GMI catalog), `PRICE_IN_PER_M` and `PRICE_OUT_PER_M` (from your account), and `GITHUB_TOKEN` (fine-grained, read-only, only if you fetch issues from GitHub).
2. Check `GMI_BASE_URL` against GMI's current docs.
3. Smoke test the model: `.venv/bin/pytest -m live -s`. The second test says whether the model returns a tool call. If it does not, tell me; the loop needs a model with tool calling.
4. Build the sandbox image once: `./docker/build.sh`.
5. Try the bundled fixture without GitHub:
   Copy `tests/fixtures/sample_repo` somewhere under your home folder, run `git init` and commit it, then
   `repopilot run --repo <copy> --issue 1 --issue-file issue.json --allow-unpriced`
   with `issue.json` = `{"title": "average_price crashes on an empty cart", "body": "average_price([]) raises ZeroDivisionError; it should return 0."}`.
6. Look at the result with `repopilot show 1` and `repopilot usage`.

Costs money: a run is capped at 60,000 tokens, at most 5 runs per day, and the daily spend cap (default 5.00) blocks further runs.
