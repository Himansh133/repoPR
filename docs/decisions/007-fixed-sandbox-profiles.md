# 007: Fixed sandbox profiles, no model-written commands

**Decision:** Tests run only through named profiles defined in code. The model can request a profile by name; the backend builds the `docker run` argument list. No shell, no model-supplied arguments.

**Why:** The model reads untrusted repo text and could be steered. A command string from the model is a way to run anything. A name from a fixed list is not.

**Also decided:** no network in the container; timeouts, out-of-memory and Docker errors are `violation` (abort), not a failed test; the image has only pytest, so repos with other dependencies are out of scope for the MVP.
