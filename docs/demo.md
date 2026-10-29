# Demo script

A five-minute walkthrough. Part 1 costs nothing. Part 2 calls the model (well under 0.01 per run at current prices). Part 3 creates a real draft PR.

## Before you start
- Setup from the README is done, `.env` is filled in (`docs/live-testing.md`), and `./docker/build.sh` has been run.
- `REPOPILOT_API_KEY` is set in `.env` (at least 16 characters).
- The dashboard is built: `cd dashboard && npm ci && npm run build`.
- For part 3: a repo you own on GitHub with an easy issue, a local clone of it, and `GITHUB_WRITE_TOKEN` (fine-grained, that repo only, Contents and Pull requests read and write).

## Part 1: what is already there (no cost)
1. `repopilot runs`: past runs with status, tokens and cost.
2. `repopilot show <id>` on a passed run: the plan, the diff, tests before and after, and the honest final status.
3. `repopilot usage --today`: runs and spend against the daily limits.
4. `repopilot compare evals/results/20261027-213937`: the benchmark table.

## Part 2: a run in the dashboard
1. `repopilot serve`, then open `http://127.0.0.1:8000/ui/`.
2. Paste `REPOPILOT_API_KEY`. Say: the key stays in this tab only and goes out only as a header.
3. **New run**: the repo (local clone path or `https://github.com/<owner>/<repo>`) and the issue number. Leave "allow unpriced" off. **Start run**.
4. The timeline streams live: baseline tests fail, the model searches and reads, submits a plan that cites the files it read, proposes a patch, the backend applies it and runs the tests. Say: the model never runs anything; the backend does.
5. When it says "Finished: passed": **Diff** tab (the whole change), **Tests** tab (the failing test before, passing after), **Report** tab.

## Part 3: approval and the draft PR
1. On the passed run, the **Decision** panel. Click **Approve…**. Say: nothing has happened yet.
2. Optional note; leave "close the issue" off. Click **Create draft PR**.
3. The panel shows the PR link. Open it on GitHub: a **draft** from `repopilot/issue-<n>-run-<id>` whose body starts with `Refs #<n>`.
4. Close or merge it yourself.

Same from the terminal: `repopilot run --repo <clone> --issue <n>`, then `repopilot approve <id>` and type `yes`.

## If something goes wrong
- "Prices are not set": add `PRICE_IN_PER_M` and `PRICE_OUT_PER_M` to `.env`, or tick "allow unpriced".
- "Daily run limit (5) reached" or "Daily spend cap ... reached": wait until tomorrow; `repopilot usage --today` shows the numbers. "Too many requests" from the API means slow down for a few seconds.
- `aborted` with a budget reason: the issue needed more investigation than the budget allows. The timeline shows how far it got.

## The recording
`docs/images/dashboard-demo.gif` was captured with headless Chrome against stored runs (run #9 and run #8), so it shows no live model call and creates no PR. Part 2 and part 3 above are the live version.
