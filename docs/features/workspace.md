# Workspace and path safety

## What it does
`app/workspace.py` represents a local repo checkout. `Workspace(root)` has:
- `resolve(rel_path)`: turns a repo-relative path into a real path, or raises `PathSafetyError`.
- `read_file(rel_path)`: bounded text read, returns `ReadResult(path, content, truncated, size_bytes)`.
- `Workspace.clone(url, dest)`: shallow clone of a public GitHub repo.

## How to use
```python
ws = Workspace("/path/to/repo")            # or Workspace.clone("https://github.com/o/r", dest)
ws.read_file("src/app.py").content
```

## Rejected paths
Absolute paths, any `..` segment, empty or non-string paths, null bytes, and symlinks (file or directory) whose real target is outside the root. Symlinks that stay inside the repo are fine.

## Design notes
- Rule is simple on purpose: any `..` is refused, even if it would stay inside the repo.
- After the checks, the path is resolved (symlinks followed) and must still be under the root.
- Reads take at most `max_file_bytes` (default 50,000, `Settings.max_file_bytes`). Larger files return the first part with `truncated=True`. Cut multibyte characters are replaced, not crashed on.
- Binary files (NUL byte in the first 8 KB) are refused.
- Error messages use the repo-relative path only, never the absolute path.
- Clone accepts only `https://github.com/<owner>/<repo>[.git]`, runs `git clone --depth 1 -- URL DEST` as an argument list (no shell), with a 120 s timeout and prompts disabled.

## Limits
- Text files only. Files are read as UTF-8.
- Clone is for public repos; no auth.

## How it is tested
`tests/test_workspace.py`: traversal, absolute, null byte, empty, symlink file and directory escape, inside-repo symlink, size limit, exact limit, multibyte cut, binary, missing, directory, no path leak, and clone URL validation (clone command is mocked, no network).
