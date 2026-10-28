"""Docker sandbox for running tests.

The model never supplies a command. It can only name an approved profile, and the
profile's command is fixed here. Containers run with no network, a non-root user, a
read-only filesystem and mount, dropped capabilities, and CPU/memory/process/time limits.

A profile may also have a fixed install command (`npm ci --ignore-scripts`). It runs once,
before the tests, in its own container: that one has network and a writable mount so it
can fill `node_modules`, but runs no package scripts. The tests still run offline.
"""

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

MAX_LOG_CHARS = 20_000
MAX_RAW_BYTES = 200_000
MAX_FAILED_NAMES = 50
MAX_NAME_CHARS = 200
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
# node --test (TAP and spec reporters), vitest/jest "FAIL <file> > <name>", jest "● <name>"
_NODE_FAILED_LINE = re.compile(
    r"^(?:not ok \d+ - (.+)|\s*✖ (.+?)(?: \([\d.]+m?s\))?|\s*FAIL\s+(.+)|\s*● (.+))$",
    re.MULTILINE,
)
_NO_NPM_TEST = ('Missing script: "test"', "Error: no test specified")
_PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini",
                   "requirements.txt")
INSTALL_MEMORY = "2g"
INSTALL_TMPFS = "1g"


class SandboxError(Exception):
    """Docker is missing or cannot start, or the request is not allowed."""


class ExecStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    VIOLATION = "violation"  # timeout, out of memory or a sandbox error: abort the run


@dataclass(frozen=True)
class SandboxProfile:
    """An approved way to run tests. The command is fixed; the model only picks the name."""

    name: str
    image: str
    command: tuple[str, ...]
    timeout_s: int = 60
    memory: str = "512m"
    cpus: str = "1"
    pids: int = 128
    tmpfs_size: str = "64m"
    env: tuple[str, ...] = ()
    install: tuple[str, ...] | None = None  # runs once before the tests, with network
    cache_dirs: tuple[str, ...] = ()  # repo-relative; writable tmpfs during the test run
    install_timeout_s: int = 300


def default_profiles(
    image: str = "repopilot-pytest", timeout_s: int = 60, node_image: str = "repopilot-node"
):
    return {
        "python-pytest": SandboxProfile(
            name="python-pytest",
            image=image,
            command=(
                "python", "-m", "pytest", "-x", "-q", "-rf", "--tb=short", "-p", "no:cacheprovider",
            ),
            timeout_s=timeout_s,
        ),
        "node-npm": SandboxProfile(
            name="node-npm",
            image=node_image,
            command=("npm", "test"),
            timeout_s=timeout_s,
            memory="1g",
            pids=256,
            env=("CI=true", "NO_COLOR=1", "FORCE_COLOR=0", "npm_config_update_notifier=false"),
            install=("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"),
            cache_dirs=("node_modules/.vite", "node_modules/.cache"),
        ),
    }


def detect_profile(root: str | Path) -> str:
    """`node-npm` for a repo with a package.json and no Python project files, else pytest."""
    path = Path(root)
    if (path / "package.json").is_file() and not any(
        (path / m).exists() for m in _PYTHON_MARKERS
    ):
        return "node-npm"
    return "python-pytest"


@dataclass(frozen=True)
class RawRun:
    """What the Docker layer returns: exit code, merged output (tail only), and flags."""

    returncode: int | None
    output: bytes
    output_cut: bool = False
    timed_out: bool = False


@dataclass(frozen=True)
class Execution:
    id: str
    profile: str
    status: ExecStatus
    exit_code: int | None
    duration_s: float
    failed_tests: list[str] = field(default_factory=list)
    log_tail: str = ""
    truncated: bool = False
    reason: str | None = None


class DockerRunner(Protocol):
    def run(self, argv: list[str], timeout_s: float, name: str) -> RawRun: ...


def build_command(profile: SandboxProfile, workdir: Path, name: str) -> list[str]:
    """The exact `docker run` argument list for a profile. No shell is involved."""
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--user", "65534:65534",
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={profile.tmpfs_size}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", profile.memory, "--memory-swap", profile.memory,
        "--cpus", profile.cpus,
        "--pids-limit", str(profile.pids),
        "--mount", f"type=bind,source={workdir},target=/work,readonly",
        *[a for d in profile.cache_dirs
          for a in ("--tmpfs", f"/work/{d}:rw,noexec,nosuid,mode=1777,size={profile.tmpfs_size}")],
        "-w", "/work",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
        *[a for e in profile.env for a in ("-e", e)],
        profile.image,
        *profile.command,
    ]


def build_install_command(
    profile: SandboxProfile, workdir: Path, name: str, user: str
) -> list[str]:
    """The `docker run` list for a profile's install step.

    Unlike the test run it has network and a writable mount, and runs as the host user so
    the files it writes belong to them. Everything else stays locked down, and the install
    command itself (no package scripts) is fixed in the profile.
    """
    assert profile.install is not None
    return [
        "docker", "run", "--rm", "--name", name,
        "--user", user,
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={INSTALL_TMPFS}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", INSTALL_MEMORY, "--memory-swap", INSTALL_MEMORY,
        "--cpus", profile.cpus,
        "--pids-limit", str(profile.pids),
        "--mount", f"type=bind,source={workdir},target=/work",
        "-w", "/work",
        "-e", "HOME=/tmp",
        *[a for e in profile.env for a in ("-e", e)],
        profile.image,
        *profile.install,
    ]


def parse_failed_tests(log: str, profile: str = "python-pytest") -> list[str]:
    """Names of failed tests from the runner's summary lines. Deduped, capped."""
    pattern = _NODE_FAILED_LINE if profile == "node-npm" else _FAILED_LINE
    names: list[str] = []
    for match in pattern.finditer(log):
        name = next(g for g in match.groups() if g is not None).strip()[:MAX_NAME_CHARS]
        if name not in names:
            names.append(name)
        if len(names) >= MAX_FAILED_NAMES:
            break
    return names


class SubprocessDocker:
    """Runs the docker CLI. Keeps only the tail of the output, and kills on timeout."""

    def __init__(self, max_raw_bytes: int = MAX_RAW_BYTES) -> None:
        self.max_raw_bytes = max_raw_bytes

    def run(self, argv: list[str], timeout_s: float, name: str) -> RawRun:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise SandboxError("Docker is not installed or not on PATH") from None

        buf = bytearray()
        state = {"cut": False}

        def pump() -> None:
            assert proc.stdout is not None
            while chunk := proc.stdout.read1(65536):
                buf.extend(chunk)
                if len(buf) > self.max_raw_bytes:
                    del buf[: len(buf) - self.max_raw_bytes]
                    state["cut"] = True

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
        reader.join(timeout=5)
        return RawRun(
            returncode=None if timed_out else proc.returncode,
            output=bytes(buf),
            output_cut=state["cut"],
            timed_out=timed_out,
        )


class Sandbox:
    """Runs approved test profiles in Docker and remembers the results."""

    def __init__(
        self,
        docker: DockerRunner | None = None,
        profiles: dict[str, SandboxProfile] | None = None,
        max_log_chars: int = MAX_LOG_CHARS,
    ) -> None:
        self.docker = docker or SubprocessDocker()
        self.profiles = profiles if profiles is not None else default_profiles()
        self.max_log_chars = max_log_chars
        self._results: dict[str, Execution] = {}

    def prepare(self, profile_name: str, workdir: str | Path) -> float | None:
        """Run the profile's install step, if it has one. Returns its duration in seconds.

        Raises SandboxError if the install cannot run or fails.
        """
        profile, path = self._check(profile_name, workdir)
        if profile.install is None:
            return None
        if not (path / "package-lock.json").is_file():
            raise SandboxError("Node repos need a package-lock.json (npm ci installs from it)")
        name = f"repopilot-{uuid.uuid4().hex[:12]}"
        user = f"{os.getuid()}:{os.getgid()}"
        started = time.monotonic()
        raw = self.docker.run(
            build_install_command(profile, path, name, user), profile.install_timeout_s, name
        )
        duration = time.monotonic() - started
        if raw.timed_out:
            raise SandboxError("dependency install exceeded the time limit")
        if raw.returncode != 0:
            text = raw.output.decode("utf-8", errors="replace").strip()
            tail = " | ".join(text.splitlines()[-3:])[:300]
            raise SandboxError(f"dependency install failed (exit {raw.returncode}): {tail}")
        for d in profile.cache_dirs:  # mount points for the test run's tmpfs caches
            target = path / d
            if not target.resolve().is_relative_to(path):
                raise SandboxError(f"{d} points outside the repository")
            target.mkdir(parents=True, exist_ok=True)
        return duration

    def run_tests(self, profile_name: str, workdir: str | Path) -> Execution:
        """Run a profile on a directory. Raises SandboxError if it cannot even start."""
        profile, path = self._check(profile_name, workdir)
        name = f"repopilot-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        raw = self.docker.run(build_command(profile, path, name), profile.timeout_s, name)
        duration = time.monotonic() - started

        text = raw.output.decode("utf-8", errors="replace")
        if raw.returncode == 125:  # docker itself could not start the container
            first = (text.strip().splitlines() or ["docker failed"])[0]
            raise SandboxError(f"Docker could not start the container: {first[:200]}")

        if profile.name == "node-npm":
            status, reason = self._classify_npm(raw, text)
        else:
            status, reason = self._classify(raw)
        result = Execution(
            id=uuid.uuid4().hex[:12],
            profile=profile.name,
            status=status,
            exit_code=raw.returncode,
            duration_s=duration,
            failed_tests=parse_failed_tests(text, profile.name),
            log_tail=text[-self.max_log_chars :],
            truncated=raw.output_cut or len(text) > self.max_log_chars,
            reason=reason,
        )
        self._results[result.id] = result
        return result

    def get_results(self, execution_id: str) -> Execution | None:
        """Look up an earlier run by id."""
        return self._results.get(execution_id)

    def _check(self, profile_name: str, workdir: str | Path) -> tuple[SandboxProfile, Path]:
        profile = self.profiles.get(profile_name) if isinstance(profile_name, str) else None
        if profile is None:
            raise SandboxError(
                f"Unknown test profile '{str(profile_name)[:50]}'. "
                f"Available: {', '.join(self.profiles)}"
            )
        path = Path(workdir)
        if not path.is_dir():
            raise SandboxError("Working directory does not exist")
        path = path.resolve()
        if any(c in str(path) for c in ",:\n"):
            raise SandboxError("Working directory path contains unsupported characters")
        return profile, path

    @staticmethod
    def _classify_npm(raw: RawRun, text: str) -> tuple[ExecStatus, str | None]:
        """npm exits 1 for any test failure; pytest's other exit codes mean nothing here."""
        if raw.timed_out or raw.returncode in (0, 137):
            return Sandbox._classify(raw)
        if any(marker in text for marker in _NO_NPM_TEST):
            return ExecStatus.INCONCLUSIVE, "the repo has no npm test script"
        return ExecStatus.FAILED, None

    @staticmethod
    def _classify(raw: RawRun) -> tuple[ExecStatus, str | None]:
        if raw.timed_out:
            return ExecStatus.VIOLATION, "timeout: tests exceeded the time limit"
        code = raw.returncode
        if code == 0:
            return ExecStatus.PASSED, None
        if code in (1, 2):  # 2 includes collection errors, e.g. a patch that broke an import
            return ExecStatus.FAILED, None
        if code == 5:
            return ExecStatus.INCONCLUSIVE, "no tests were collected"
        if code in (3, 4):
            return ExecStatus.INCONCLUSIVE, "pytest internal or usage error"
        if code == 137:
            return ExecStatus.VIOLATION, "container was killed (out of memory?)"
        return ExecStatus.VIOLATION, f"unexpected exit code {code}"
