# 006: Apply patches in a temp clone on a new branch

**Decision:** The agent works in a `git clone --local` copy in a temp dir, on branch `repopilot/issue-<n>`. Each accepted patch is one commit. The final diff is `git diff <base> HEAD`.

**Why:** The user's checkout stays untouched even if something goes wrong. Commits give an exact record, make repairs stack cleanly, and give the later PR step a ready branch. The sandbox mounts the copy.

**Cost:** Only committed state is copied. Uncommitted work in the source is ignored.
