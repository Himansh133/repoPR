# search_code and read_file tools

## What they do
- `search_code(ws, query, path=None, max_matches=20, max_snippet_chars=200)` returns `SearchResult(matches, truncated)`. Each `Match` has a repo-relative `path`, a `line` number and a capped `text`.
- `read_file(ws, path)` returns a bounded `ReadResult` (see `workspace.md`).

## How to use
```python
ws = Workspace("/path/to/repo")
result = search_code(ws, "average_price", path="shop")
for m in result.matches:
    print(m.path, m.line, m.text)
```

## Design notes
- Search is **literal**, not regex. Simple and safe; the model searches for names and strings.
- Uses ripgrep if installed, else a pure-Python fallback with the same rules. Both run the same tests.
- ripgrep runs as a fixed argument list (no shell). The query follows `-e`, so `--files` or `-e` are plain text. A path is always passed so ripgrep never reads stdin. `--sort path` keeps results in a stable order.
- Limits: query max 200 characters, no null bytes; 20 matches by default; 5 matches per file; snippet 200 characters; files over 1 MB and binary files skipped; hidden folders, `.git`, `node_modules` and `__pycache__` skipped; 10 s timeout.
- `truncated=True` means more matches existed than were returned.
- Symlinks are not followed in either backend. A `path` argument goes through `Workspace.resolve`.
- Text found in files (README, comments) is returned as plain data. Nothing in the tool acts on it.

## Limits
- No regex, no context lines, no ignore-file support in the fallback.
- The cap on files read per investigation (10) belongs to the agent loop.

## How it is tested
`tests/test_tools_search.py` runs every test against the fixture repo `tests/fixtures/sample_repo/` with both backends: normal search, no match, literal matching, subfolder and single file, binary skipped, match limit, snippet cap, flag-like queries, bad queries, paths outside the root, symlink not followed, injection text treated as data. The fixture has a deliberate bug (`average_price([])`) and prompt-injection text in `README.md` and `shop/notes.py`.
