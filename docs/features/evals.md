# Evaluation harness

## What it does
Runs a benchmark of issues against one or more models and compares them. `repopilot eval` runs it; `repopilot compare <dir>` reprints the table from saved results.

```
repopilot eval --models modelA,modelB [--issues evals/benchmark_issues.jsonl] [--out DIR]
               [--base-dir .] [--max-runs 30] [--allow-unpriced]
repopilot compare evals/results/20261027-101500
```
Run it from the project root (`dir:` repos are relative to `--base-dir`).

## Benchmark file
`evals/benchmark_issues.jsonl`, one JSON object per line:
```json
{"id": "avg-price-empty-cart", "repo": "dir:evals/repos/avg_price", "commit": null,
 "issue": {"title": "...", "body": "..."},
 "relevant_test": "tests/test_cart.py::test_average_price_of_empty_cart_is_zero",
 "expected_outcome": "passed", "notes": ""}
```
- `repo`: `dir:<path>` (a folder in this project, turned into a fresh git repo for each run; `commit` must be null) or `https://github.com/<owner>/<repo>` with a `commit` (7-40 hex characters, fetched with fixed git commands).
- `relevant_test`: the test that shows the issue is fixed.
- `expected_outcome`: `passed`, `failed` or `inconclusive`. **Write it before you run anything.** The results record the SHA-256 of the benchmark file, so you can show it was not edited afterwards (commit the file before running).
- Validation names the line and field. Max 100 issues, unique ids, no duplicates.

Three seed issues ship in `evals/repos/` (`avg_price`, `clamp`, `word_count`), each with a small bug and a failing test. Add your real public-repo issues (aim for 10, then 20 to 30) to the same file.

## What a run saves
`<out>/<model>/<issue-id>/`: `patch.diff`, `trace.json` (events), `tests.json` (baseline and after each patch, with log tails), `result.json`. In `<out>`: `results.json`, `summary.json`, `comparison.md`, `meta.json` (models, issues, budgets, benchmark hash, why it stopped early). Each run is also stored in the database, so `repopilot usage` includes it.

## Metrics
| Metric | Meaning |
|---|---|
| Resolved (issue resolution rate) | runs that ended `passed` with the relevant test failing before and passing now, over all runs |
| Patch test pass | of the runs that made a patch, how many had the last test run pass |
| Tool-call success | successful tool results, accepted plans and applied patches over all of them |
| Repair success | of runs that needed at least one repair, how many ended `passed` |
| Cost / issue | mean cost over runs with a known cost (the count of unknown ones is shown) |
| Median latency | wall-clock seconds per run |
| Matched expectation | runs whose status equals `expected_outcome` |

A metric with nothing to measure prints `n/a`, not 0%.

## Safety and cost
- Runs go one at a time, through the same sandbox and budgets as `repopilot run`.
- `--max-runs` (default 30) refuses to start a benchmark larger than you meant.
- The daily spend cap applies: when it is reached the remaining runs are skipped and `meta.json` says why. The 5-runs-a-day limit does not apply to `eval`, because a benchmark is deliberate.
- A crash in one run is recorded as status `error` and the rest continue. Temp repos and working copies are removed.

## How it is tested
`tests/test_evals.py` (38 tests): loading and every validation error, the shipped seed file, preparing repos (and rejecting paths outside the project), fixed git commands for commit checkout, evaluating runs, exact metric numbers on hand-made results, the markdown table, a run with artifacts and database rows, two models, a crash, the spend cap, cleanup, and saving and loading results. `tests/test_cli.py` covers `eval` and `compare`.
