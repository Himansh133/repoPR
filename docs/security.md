# Security

Safety controls and the tests that cover them. One line per control.

- Secrets live only in `.env`, which is git-ignored. `.env.example` has placeholders. (Chunk 0)
- Tests that call the network or a real LLM are skipped by default (`live`, `docker` markers). (Chunk 0)
- API key and GitHub token are `SecretStr`; tests check they do not appear in `repr` or error messages. (Chunk 1)
- LLM calls are bounded: timeout, max 3 retries, max output tokens 1,200. 401/403 is never retried. (Chunk 1)
- Default tests mock all HTTP with `respx`; real calls only under `-m live`. (Chunk 1)
- All file access goes through `Workspace.resolve`: rejects absolute paths, `..`, null bytes, and symlink escapes (file and directory). Tested. (Chunk 2A)
- File reads are size-bounded; binary files are refused; errors never contain absolute paths. (Chunk 2A)
- Clone allows only `https://github.com/<owner>/<repo>`, uses a fixed argument list with `--`, no shell, with a timeout. (Chunk 2A)
- GitHub client is read-only, validates owner/repo/number before any request, caps issue and comment size, rejects pull requests, and never logs the token. Tested. (Chunk 2B)
