# Docker sandbox

## What it does
`app/sandbox.py` runs tests in a disposable Docker container. `Sandbox.run_tests(profile_name, workdir)` returns an `Execution(id, profile, status, exit_code, duration_s, failed_tests, log_tail, truncated, reason)`. `Sandbox.get_results(id)` looks up an earlier run.

## Setup (once)
```
./docker/build.sh        # builds the repopilot-pytest image (python:3.12-slim + pytest), ~210 MB
```
The build needs network. The containers it makes run with none.

## Profiles
A profile is a fixed image + command + limits. The model can only name a profile; it never supplies command text. One profile exists: `python-pytest`, which runs `python -m pytest -x -q -rf --tb=short -p no:cacheprovider`. Unknown names are rejected before Docker is touched.

## Container flags
`--network none`, `--user 65534:65534`, `--read-only` root, `--tmpfs /tmp` (64 MB, noexec), `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory 512m` (no swap), `--cpus 1`, `--pids-limit 128`, the repo mounted read-only at `/work`, no host environment, no other mounts, no shell. Timeout 60 s (setting `SANDBOX_TIMEOUT_S`). Image name: `SANDBOX_IMAGE`.

## Status mapping
| Result | Status |
|---|---|
| exit 0 | `passed` |
| exit 1, or 2 (includes collection errors from a broken import) | `failed` |
| exit 5 (no tests collected), 3, 4 (pytest internal/usage) | `inconclusive` |
| timeout, exit 137 (out of memory), other exit codes | `violation` (the run must abort) |
| docker missing or could not start the container (exit 125) | raises `SandboxError` |

## Logs
Output is merged (stdout + stderr). The runner keeps only the last 200 KB while reading, so a noisy test cannot fill memory. The result keeps the last 20,000 characters, and `truncated` says if anything was cut. Failed test names come from pytest's `FAILED x::y` and `ERROR x` lines (deduplicated, max 50).

## Design notes
- On timeout the runner runs `docker rm -f <name>` (kills and removes, synchronously) and kills the CLI's process group.
- The working directory must be on a path Docker can mount. Docker Desktop (the `desktop-linux` context) runs in a VM that shares the home folder but **not `/tmp`**, so working copies live in `~/.cache/repopilot/work` (see `default_work_dir`).
- Repos whose tests need packages beyond pytest and the standard library will fail in this MVP, because the image has no other packages and the container has no network.

## How it is tested
- `tests/test_sandbox.py` (no Docker): exact command, flags, unknown profile, bad paths, exit-code mapping, timeout, log tail, name parsing, results store, and the real runner against a stub `docker` script (exit codes, `rm -f` on timeout, bounded memory for huge output, Docker missing).
- `tests/test_sandbox_docker.py` (marker `docker`, run with `pytest -m docker`): real container on the fixture repo: the bug is reported by test name, a fixed copy passes, the source directory is unchanged, network is off, mount is read-only and user is not root, timeout kills the container, 1 MB of output is truncated, out of memory is a violation, no tests is inconclusive.
