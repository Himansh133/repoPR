# Full run: patch, test, repair, report

## What it does
`run_issue(llm, source, issue, sandbox, budgets)` in `app/agent/fix.py` runs the whole workflow on a temp clone of the repo and returns a `RunResult`. `build_report(result, issue)` in `app/report.py` turns it into plain text.

## Flow
```
INIT -> INVESTIGATE (baseline tests, then read-only investigation) -> PLAN
     -> PATCH -> VALIDATE -> TEST -> REPORT
                              |
                              +-> REPAIR (max 2) -> VALIDATE -> TEST ...
any working state -> ABORTED
```
1. **Clone:** `WorkingCopy.create` makes the temp clone on `repopilot/issue-<n>`. A failure here raises `PatchError` before the run starts.
2. **Baseline:** the approved test profile runs on the unpatched copy. A sandbox violation aborts before any model call.
3. **Investigate:** unchanged from the investigation loop; ends with an evidence-backed plan. The patch phase starts a fresh conversation with the issue, the accepted plan and the names of the tests that failed at baseline.
4. **Patch:** the model has `search_code`, `read_file` and `propose_patch(diff)`. The backend validates the diff, applies it and commits it. A rejected patch goes back to the model as an error and counts as an invalid call (3 in a row abort). Several tool calls in one reply: after a successful patch the rest get a "Skipped" answer.
5. **Test:** the **backend** runs the tests after every applied patch. The model cannot run tests or commands (`run_tests` and `run_shell` are refused as unknown tools).
6. **Repair:** on failure the model gets only the failed test names and the last 3,000 characters of output, inside `<untrusted_test_output>`, as the result of its `propose_patch` call. Patches stack on the current branch. At most 2 repairs.
7. **Report:** `RunResult.final_diff` is `git diff <base> HEAD`.

## Final status
| Status | When |
|---|---|
| `passed` | the baseline failed, and after the patch all tests pass |
| `failed` | tests still fail after the allowed repairs |
| `inconclusive` | tests pass but were already passing before the patch; or no tests were collected; or a patch left no tests to run |
| `aborted` | sandbox violation (timeout, out of memory), sandbox error, step/token/time budget, LLM error, investigation failure, or 3 invalid calls in a row |

`result.reason` explains the status. Only `passed` means the fix is shown to work.

## Budgets (settings)
Whole run: 24 steps (`AGENT_RUN_MAX_STEPS`), 60,000 tokens (`AGENT_RUN_MAX_TOKENS`), 300 s (`AGENT_RUN_TIMEOUT_S`), 2 repairs (`AGENT_MAX_REPAIRS`). The investigation phase has its own smaller share (12 steps, 30,000 tokens) taken out of the same totals.

## Report
`## Plan`, `## Proposed changes` (branch, files, +/-, diff capped at 8,000 characters), `## Test results` (before the patch, after each patch, repair attempts used), `## Unresolved`, `## Usage`. Proposed changes, passed/failed tests and unresolved items are kept apart. An aborted run says "No patch was produced" and "Run stopped early" as appropriate.

## Design notes
- The working copy is **kept** after the run (`result.workdir`) so the PR step can use the branch. The caller removes it.
- Events record names, counts, sizes and statuses, never diffs, file contents or test logs.
- Limits: one profile (`python-pytest`), repos with only pytest and stdlib dependencies, Python only.

## How it is tested
- `tests/test_fix_run.py` (fake LLM and fake sandbox, real git clone): pass on first try, fail then repair then pass, fail after 2 repairs, violation aborts immediately, baseline violation aborts before any model call, sandbox errors, investigation failure, LLM error, shared step/token/time budgets, baseline already passing, no tests collected, invalid patches looping back, 3 invalid patches abort, model cannot run tests, placeholder answers, prompt contents, events hold no diffs or logs.
- `tests/test_report.py`: each status, sections, truncation, never says PASSED unless passed.
- `tests/test_fix_run_docker.py` (marker `docker`): real container, scripted model: a good fix passes; a wrong fix is caught and the repair accepted.
