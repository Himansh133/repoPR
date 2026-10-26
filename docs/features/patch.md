# Patch validation and working copy

## What it does
`app/patch.py` turns a model-written unified diff into a committed change on a new branch, in a disposable copy of the repo.
- `parse_diff(diff)` parses and validates the diff without touching any file.
- `WorkingCopy.create(source, issue_number)` makes a temp `git clone --local` of the committed state and a branch `repopilot/issue-<n>`. Your own checkout is never touched. Uncommitted changes in the source are not copied.
- `WorkingCopy.apply_patch(diff)` validates, runs `git apply --check`, applies, and commits as "RepoPilot". It returns `PatchResult(files, added, removed, branch, commit_sha, diff)`.
- `WorkingCopy.final_diff()` is the whole branch against where it started (patches stack, so repairs apply on top).
- `WorkingCopy.cleanup()` deletes the copy.

## Rules for a diff
Allowed: modify an existing file, or create a new one. Max 20 KB and 5 files.
Rejected: deletes, renames, copies, mode changes, binary patches, paths with `..`, absolute paths, backslashes, null bytes, `.git` anywhere, `.github/`, headers without `a/` and `b/`, and paths that go through a symlink. A missing file (for a modify) or an existing file (for a create) is also rejected.

## Design notes
- The parser counts hunk lines using the numbers in each `@@` header, so a removed line like `--- text` is not mistaken for a new file header. Short hunks, long hunks, stray prose and missing headers are all errors with a line number.
- A missing final newline is added before applying, since models often leave it off.
- Git runs as a fixed argument list, no shell, hooks disabled (`core.hooksPath=/dev/null`), no prompts, 60 s timeout. A hook planted in the copy does not run (tested).
- On failure nothing changes: the check runs before applying, and a failure after applying resets and cleans the tree.
- Error messages are short and replace the temp path with `<repo>`.

## Limits
- Patches can edit tests, which allows adding a regression test.
- The source must be a git repo with its own commits.

## How it is tested
`tests/test_patch.py`, real git in a temp dir: good and multi-file diffs, new files, git-style headers, line counting, malformed diffs, unsafe paths, deletes/renames/mode/binary, size and file limits, applying and committing, stacking patches, a patch that does not apply, symlink paths, hooks not run, no path leaks, source untouched.
