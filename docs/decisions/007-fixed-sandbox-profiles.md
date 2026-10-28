# 007: Fixed sandbox profiles, no model-written commands

**Decision:** Tests run only through named profiles defined in code. The model can request a profile by name; the backend builds the `docker run` argument list. No shell, no model-supplied arguments.

**Why:** The model reads untrusted repo text and could be steered. A command string from the model is a way to run anything. A name from a fixed list is not.

**Also decided:** no network in the container; timeouts, out-of-memory and Docker errors are `violation` (abort), not a failed test; the image has only pytest, so repos with other dependencies are out of scope for the MVP.

**Amended (Node support):** a second profile, `node-npm`, runs `npm test`. Node repos need their dependencies, and a per-repo set cannot be baked into the image, so a profile may have a fixed install command. For `node-npm` it is `npm ci --ignore-scripts --no-audit --no-fund`. It runs once before the baseline, in its own container that has network and a writable mount but otherwise the same limits, and it runs no package scripts. The tests themselves still run with no network and a read-only mount. A repo without `package-lock.json` is refused, so only locked versions are installed. The profile is picked by the backend from the repo's files (`SANDBOX_PROFILE=auto`), never by the model.
