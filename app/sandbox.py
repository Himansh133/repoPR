"""Docker sandbox for running tests.

The model never supplies a command. It can only name an approved profile, and the
profile's command is fixed here. Containers run with no network, a non-root user, a
read-only filesystem and mount, dropped capabilities, and CPU/memory/process/time limits.
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


def default_profiles(image: str = "repopilot-pytest", timeout_s: int = 60):
    return {
        "python-pytest": SandboxProfile(
            name="python-pytest",
            image=image,
            command=(
                "python", "-m", "pytest", "-x", "-q", "-rf", "--tb=short", "-p", "no:cacheprovider",
            ),
            timeout_s=timeout_s,
        )
    }


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
        "-w", "/work",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
        profile.image,
        *profile.command,
    ]


def parse_failed_tests(log: str) -> list[str]:
    """Names from pytest `FAILED x::y` / `ERROR x` summary lines. Deduped, capped."""
    names: list[str] = []
    for match in _FAILED_LINE.finditer(log):
        name = match.group(1)[:MAX_NAME_CHARS]
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

    def run_tests(self, profile_name: str, workdir: str | Path) -> Execution:
        """Run a profile on a directory. Raises SandboxError if it cannot even start."""
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

        name = f"repopilot-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        raw = self.docker.run(build_command(profile, path, name), profile.timeout_s, name)
        duration = time.monotonic() - started

        text = raw.output.decode("utf-8", errors="replace")
        if raw.returncode == 125:  # docker itself could not start the container
            first = (text.strip().splitlines() or ["docker failed"])[0]
            raise SandboxError(f"Docker could not start the container: {first[:200]}")

        status, reason = self._classify(raw)
        result = Execution(
            id=uuid.uuid4().hex[:12],
            profile=profile.name,
            status=status,
            exit_code=raw.returncode,
            duration_s=duration,
            failed_tests=parse_failed_tests(text),
            log_tail=text[-self.max_log_chars :],
            truncated=raw.output_cut or len(text) > self.max_log_chars,
            reason=reason,
        )
        self._results[result.id] = result
        return result

    def get_results(self, execution_id: str) -> Execution | None:
        """Look up an earlier run by id."""
        return self._results.get(execution_id)

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
