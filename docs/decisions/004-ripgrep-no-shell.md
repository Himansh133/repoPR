# 004: Search with ripgrep as an argument list, with a Python fallback

**Decision:** `search_code` runs ripgrep as a fixed argument list, never through a shell, with the query after `-e`. If ripgrep is not installed, a pure-Python search with the same limits is used.

**Why:** The query comes from the model, which may be steered by untrusted repo text. A shell or an unguarded flag could run something unexpected. Literal search plus `-e` removes both risks. The fallback means the project works on a machine without ripgrep, and the tests cover both.

**Cost:** Two code paths to keep in sync. The shared test suite checks they behave the same.
