# SQLite persistence and usage tracking

## What it does
`app/db.py` keeps every run in one SQLite file (`~/.cache/repopilot/repopilot.db`, or `DB_PATH`). The file is created with mode 0600.

## API
```python
db = Database(path)
run_id = db.start_run(repo, issue_number, issue_title, model)   # counts toward daily limits now
record = db.finish_run(run_id, result, report_text, prices=(price_in, price_out))
db.fail_run(run_id, "reason")                                    # run crashed before a result
db.get_run(id); db.list_runs(limit); db.get_events(id); db.get_patches(id)
db.get_executions(id); db.get_usage(id); db.usage_totals(day=None)
db.check_can_start(spend_cap, max_runs, priced, allow_unpriced)  # raises BudgetBlocked
db.record_approval(id, "approved"|"rejected", note); db.get_approvals(id)
```

## Tables
`runs`, `events`, `patches`, `test_executions`, `usage_records`, `approvals`. The schema version is in `PRAGMA user_version` (1). Opening an existing file is safe; a file from a newer version is refused.

## What is and is not stored
- Stored: run summary and report, events (names, counts, sizes), patches with their diff (max 20 KB, model-written), test status, exit code and failed test names, one usage record per LLM call.
- **Not stored:** test logs, file contents, secrets. Tested.

## Cost
Per call: `(in * price_in + out * price_out) / 1,000,000` as exact `Decimal`, stored as text. The run cost is the sum. If a price is missing the cost is `NULL` (unknown), never 0. If the provider omitted token usage for any call, the run is flagged `cost_estimated`.

## Daily limits (UTC day)
`check_can_start` raises `BudgetBlocked` when:
- prices are not set and `allow_unpriced` is false (spend cannot be measured);
- today's run count has reached `MAX_RUNS_PER_DAY` (default 5; running and crashed runs count);
- today's spend has reached `DAILY_SPEND_CAP` (default 5.00; `None` turns the spend cap off).

## How it is tested
`tests/test_db.py`: schema and reopening, file mode, persistence across connections, run rows, child rows, no test logs in the file, exact cost (0.006 + 0.0048 = 0.0108), estimates, unknown prices, totals per day and overall, every limit and the day boundary, approvals.
