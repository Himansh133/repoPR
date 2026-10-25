# 005: The plan is submitted through a validated pseudo-tool

**Decision:** The model ends the investigation by calling `submit_plan` with structured arguments. The backend validates it and checks that every cited file was read.

**Why:** Parsing a plan out of free text is fragile and cannot prove which files the model saw. A structured call is validated like any other tool, and the backend already knows the exact set of files read. This enforces "never claim to have inspected files you have not read" in code, not just in the prompt.

**Cost:** The model must support tool calling. If the live probe shows it does not, the fallback is structured JSON output, and only this loop's parsing changes.
