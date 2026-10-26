# 010: argparse CLI with injected dependencies

**Decision:** The CLI uses the standard-library `argparse`. Every collaborator (settings, LLM client, sandbox, GitHub client, clone, output streams) is passed in through a `Deps` object with real defaults.

**Why:** No extra dependency, and the whole command can be tested end to end with fakes, with no network, Docker or model. Read-only commands do not need a model key, so they use a small separate settings class.

**Cost:** Exit code 2 belongs to argparse, so "inconclusive" is 3, "aborted" is 4 and "usage or budget error" is 5.
