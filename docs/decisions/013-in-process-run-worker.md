# 013: Runs in a worker thread inside the API process

**Decision:** `POST /runs` reserves a single run slot, then runs the agent in a background thread with its own event loop. Events are kept in memory for streaming and saved to SQLite when the run ends. No job queue.

**Why:** The sandbox call is synchronous and slow, so it cannot run on the server's event loop. One run at a time matches the sandbox, the budgets and the daily limits, which all assume one run. A queue (Celery, RQ, arq) would add a broker and a dependency for no gain at this size.

**Cost:** If the server stops mid-run, that run is lost; at the next startup, runs left `running` for over an hour are marked `error`. Live events exist only in the process that ran them; other processes see the stored events after the run ends.
