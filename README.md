# RepoPilot

An AI agent that takes a GitHub issue, proposes a minimal patch, and proves it with tests in a Docker sandbox. It never creates a pull request without explicit approval.

## Project status

| Chunk | Name | Status | Tests |
|---|---|---|---|
| 0 | Project scaffold | Done | 2 |
| 1 | Config and GMI client | Mocked tests done, live smoke test pending | 22 (+2 live) |
| 2 (A) | Workspace and path safety | Done | 63 total |
| 2 (B) | GitHub issue fetching | Done | 84 total |
| 2 (C) | search_code and read_file tools | Done | 134 total (109 without ripgrep installed) |
| 3 (A) | Tool schemas and dispatcher | Done | 197 total (144 without ripgrep installed) |
| 3 (B) | State machine and investigation loop | Done | 243 total (171 without ripgrep installed) |
