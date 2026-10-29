# Draft PR with an approval gate

A passed run becomes a draft pull request only after you approve it. Nothing is ever merged.

## Use
```
repopilot show 12                          # read the report first
repopilot approve 12                       # shows target, branch, files; asks you to type "yes"
repopilot approve 12 --yes --note "ok"     # no prompt
repopilot approve 12 --closes-issue        # body says "Fixes #N" instead of "Refs #N"
repopilot reject 12 --note "wrong fix"     # recorded; nothing is sent to GitHub
```
Needs `GITHUB_WRITE_TOKEN` in `.env`: a fine-grained token for the one repo, with **Contents: read and write** and **Pull requests: read and write**, nothing else. `GITHUB_TOKEN` stays read-only and is not used here. `approve` does not need a GMI key.

Exit codes: 0 PR created, 1 you did not type `yes`, 5 refused or GitHub failed.

## What `approve` checks, before any GitHub call
- The run exists, its status is `passed`, it has no PR yet, and it was not rejected.
- The run is linked to a GitHub repo (run from a GitHub URL, or a local repo whose `origin` is on GitHub).
- The working copy still exists, its HEAD is the last tested patch commit, and it has no uncommitted edits. So the PR holds exactly what the tests ran on.
- The changes only add or modify regular files (no deletes, renames, symlinks), paths pass the same checks as patches, at most 15 files of 1 MB each.
- `GITHUB_WRITE_TOKEN` is set.

Then it prints the target repo, the new branch, the files and the `Refs`/`Fixes` keyword, and asks you to type `yes`. Anything else sends nothing.

## What happens on GitHub
1. `GET /repos/o/r`: default branch; stop if the token has no push access.
2. `GET .../git/commits/<base>`: the commit the run started from must be on GitHub (push it first if not).
3. `GET .../git/ref/heads/<branch>`: the branch must not exist. It is never overwritten.
4. One blob per file, one tree on top of the base tree, one commit whose parent is the base.
5. Create `refs/heads/repopilot/issue-<n>-run-<id>`.
6. Open the PR on the default branch with `draft: true`.

If step 6 fails, the new branch is deleted. The approval is recorded before step 1; the PR URL is saved after step 6 and shown by `repopilot show`.

## PR content
- Title: `Fix #N: <issue title>`, one line, max 120 characters.
- Body: `Refs #N` (or `Fixes #N`), a note that RepoPilot opened it after manual approval, and the full run report (plan, diff, test results, usage) in a collapsed block, capped below GitHub's 65,536-character limit.

## Design notes
- GitHub's REST API instead of `git push`: the token sits in one Authorization header and never reaches git config, a remote URL or the working copy. See decision 012.
- `app/pr.py` is separate from the read-only `app/github.py` client; only `GitHubWriter` can write.
- Only branches named `repopilot/...` can be created. Base commits must be full SHAs.
- Error messages never contain the token. Tested.
- The database moved to schema version 2 (`runs.pr_url`). Version 1 files are migrated in place, one transaction per step, and keep their runs.

## Limits
- One PR per run. To change a fix, start a new run.
- Branch protection, required reviews and CI are left to GitHub.
- Tried against real GitHub on 2026-10-28: runs #9 and #10 became draft PRs #2 and #4 on `Himansh133/repopilot-demo`. The automated tests still mock every call.

## How it is tested
- `tests/test_pr.py` (16 tests): changes collected from a real git working copy; a moved HEAD, uncommitted edits, deletes and a missing working copy are refused; `Refs`/`Fixes`, body cap, title; the exact GitHub calls and payloads with `draft: true`; branch deleted when the PR fails; a missing base commit, an existing branch, no push access and an unknown repo stop before any write; bad input sends no request; the token never leaks.
- `tests/test_cli.py` (15 approve/reject tests): a PR from the tested working copy with the right base and files; `--closes-issue`; anything but `yes` sends nothing; reject records and blocks a later approve; only passed runs; one PR per run; missing token, removed or edited working copy and non-GitHub runs refused; a GitHub failure saves no PR; `show` prints the PR link.
- `tests/test_db.py`: a version 1 database migrates to version 2 and keeps its runs; `set_pr_url`.
