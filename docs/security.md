# Security

Safety controls and the tests that cover them. One line per control.

- Secrets live only in `.env`, which is git-ignored. `.env.example` has placeholders. (Chunk 0)
- Tests that call the network or a real LLM are skipped by default (`live`, `docker` markers). (Chunk 0)
- API key and GitHub token are `SecretStr`; tests check they do not appear in `repr` or error messages. (Chunk 1)
- LLM calls are bounded: timeout, max 3 retries, max output tokens 1,200. 401/403 is never retried. (Chunk 1)
- Default tests mock all HTTP with `respx`; real calls only under `-m live`. (Chunk 1)
