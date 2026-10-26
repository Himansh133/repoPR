# 009: Standard-library sqlite3, no ORM, money as text

**Decision:** Use `sqlite3` directly with a small `Database` class and a schema version in `PRAGMA user_version`. Store costs as text `Decimal`.

**Why:** The data model is small and fixed. An ORM adds a dependency and hides the SQL I need to explain. Floats lose exactness for money; text `Decimal` round-trips exactly.

**Also decided:** a run is inserted when it starts, so a crash still counts toward the daily limit. Test logs are never stored. Patch diffs are stored (bounded) because the PR step and evals need them.

**Cost:** Schema changes need a hand-written migration step when the version goes up.
