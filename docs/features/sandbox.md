# Docker sandbox

## What it does
`app/sandbox.py` runs tests in a disposable Docker container. `Sandbox.prepare(profile_name, workdir)` runs the profile's install step, if it has one, before the first test run. `Sandbox.run_tests(profile_name, workdir)` returns an `Execution(id, profile, status, exit_code, duration_s, failed_tests, log_tail, truncated, reason)`. `Sandbox.get_results(id)` looks up an earlier run.

## Setup (once)
```
./docker/build.sh        # builds repopilot-pytest (python:3.12-slim + pytest) and repopilot-node (node:22-slim)
```
The build needs network. The containers it makes run with none.

## Profiles
A profile is a fixed image + command + limits. The model never names or supplies a command. Unknown names are rejected before Docker is touched.

| Profile | Command | Notes |
|---|---|---|
| `python-pytest` | `python -m pytest -x -q -rf --tb=short -p no:cacheprovider` | |
| `node-npm` | `npm test` | install step `npm ci --ignore-scripts --no-audit --no-fund`; 1 GB memory, 256 processes; `CI=true`, no colours |

`SANDBOX_PROFILE` picks one. The default, `auto`, chooses per run: `node-npm` when the repo root has a `package.json` and none of `pyproject.toml`, `setup.py`, `setup.cfg`, `pytest.ini`, `tox.ini` or `requirements.txt`; otherwise `python-pytest`. Image names: `SANDBOX_IMAGE`, `SANDBOX_NODE_IMAGE`.

## Install step (node-npm)
Runs once, before the baseline, and needs a `package-lock.json` (otherwise `SandboxError`). Its container has the default network and the repo mounted writable so `node_modules` can be filled, and runs as the host user so those files belong to them. Everything else matches the test container: read-only root, `--cap-drop ALL`, `no-new-privileges`, process limit, 2 GB memory and a 1 GB `/tmp` for the npm cache. `--ignore-scripts` means no package code runs during the install. A non-zero exit or a timeout (300 s) raises `SandboxError`, which aborts the run. Patches that add dependencies are not installed again.

Test runners write caches into `node_modules/.vite` and `node_modules/.cache`. The install step creates those folders and the test run mounts a small writable tmpfs over each, so the repo mount stays read-only.

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

For `node-npm`, npm exits 1 for any failure, so: exit 0 is `passed`; a missing `test` script or npm's default `no test specified` script is `inconclusive`; timeout and 137 are `violation`; any other exit is `failed`.

## Logs
Output is merged (stdout + stderr). The runner keeps only the last 200 KB while reading, so a noisy test cannot fill memory. The result keeps the last 20,000 characters, and `truncated` says if anything was cut. Failed test names come from pytest's `FAILED x::y` and `ERROR x` lines, or for `node-npm` from `node --test` (`not ok N - name`, `✖ name`), vitest/jest `FAIL` lines and jest `● name` lines (deduplicated, max 50).

## Design notes
- On timeout the runner runs `docker rm -f <name>` (kills and removes, synchronously) and kills the CLI's process group.
- The working directory must be on a path Docker can mount. Docker Desktop (the `desktop-linux` context) runs in a VM that shares the home folder but **not `/tmp`**, so working copies live in `~/.cache/repopilot/work` (see `default_work_dir`).
- Python repos whose tests need packages beyond pytest and the standard library will fail, because the image has no other packages and the container has no network.

## How it is tested
- `tests/test_sandbox.py` (no Docker): exact command, flags, unknown profile, bad paths, exit-code mapping, timeout, log tail, name parsing, results store, and the real runner against a stub `docker` script (exit codes, `rm -f` on timeout, bounded memory for huge output, Docker missing); the `node-npm` profile: auto-detection, offline read-only test command with cache tmpfs, install command (network, writable mount, no scripts), lockfile required, install failure and timeout, npm exit-code mapping, Node failure-name parsing.
- `tests/test_sandbox_docker.py` (marker `docker`, run with `pytest -m docker`): real container on the fixture repo: the bug is reported by test name, a fixed copy passes, the source directory is unchanged, network is off, mount is read-only and user is not root, timeout kills the container, 1 MB of output is truncated, out of memory is a violation, no tests is inconclusive.
