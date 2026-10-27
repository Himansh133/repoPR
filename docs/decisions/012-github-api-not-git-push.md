# 012: Open PRs through the GitHub API, not `git push`

**Decision:** `repopilot approve` creates the branch, commit and draft PR with GitHub's REST API (blobs, tree, commit, ref, pull). It never runs `git push`. It uses a separate `GITHUB_WRITE_TOKEN`; the read token stays read-only.

**Why:** With `git push` the token has to reach git: in a remote URL, a credential helper or git config, inside a working copy the model's patch touched. With the API it lives in one Authorization header in one process, and the code can only do the six calls it needs. It also gives one clean commit on a fresh branch, and the existing-branch check means nothing is ever overwritten.

**Cost:** More calls than a push (one per file plus five), and the commit is authored by the token's owner rather than copied from the local commit. The base commit must already be on GitHub, so you push your own branch first.
