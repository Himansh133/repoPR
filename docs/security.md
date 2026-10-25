# Security

Safety controls and the tests that cover them. One line per control.

- Secrets live only in `.env`, which is git-ignored. `.env.example` has placeholders. (Chunk 0)
- Tests that call the network or a real LLM are skipped by default (`live`, `docker` markers). (Chunk 0)
