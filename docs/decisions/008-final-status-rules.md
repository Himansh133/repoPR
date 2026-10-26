# 008: Final status rules

**Decision:** A run ends `passed`, `failed`, `inconclusive` or `aborted`. `passed` needs a baseline run where the tests fail and a final run where they all pass. If the tests already passed before the patch, or none ran, the result is `inconclusive`. A timeout, out-of-memory kill or sandbox error is `aborted`, not `failed`.

**Why:** The model must not be able to claim success; only test evidence counts. A patch cannot be shown to fix an issue if the tests never failed. A timeout is a resource-limit breach by the safety rules, so the run stops at once.

**Cost:** One extra container run per issue (the baseline). A repo with no useful failing test can never reach `passed`.

**Also decided:** the backend runs the tests after every patch; the model cannot request them. Repair feedback is only failed test names and the last 3,000 characters of output.
