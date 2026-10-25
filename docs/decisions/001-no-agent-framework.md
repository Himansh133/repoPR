# 001: No agent framework

**Decision:** Build the agent as a plain Python state machine. No LangChain or LangGraph in the MVP.

**Why:** The project must stay small enough to explain every file. A state machine gives full control over budgets, retries and safety checks, and is easy to test with a fake LLM.

**Cost:** We write the loop ourselves.
