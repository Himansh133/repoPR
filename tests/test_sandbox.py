"""Unit tests for app.sandbox. No real Docker: a fake runner, plus a stub `docker` script."""

import os
import stat
import time

import pytest

from app.sandbox import (
    ExecStatus,
    RawRun,
    Sandbox,
    SandboxError,
    SandboxProfile,
    SubprocessDocker,
    build_command,
    parse_failed_tests,
)

PROFILE = SandboxProfile(
    name="python-pytest",
    image="repopilot-pytest",
    command=("python", "-m", "pytest", "-x", "-q", "-rf", "--tb=short", "-p", "no:cacheprovider"),
    timeout_s=30,
)


class FakeDocker:
    def __init__(self, *runs):
        self.runs = list(runs)
        self.calls = []

    def run(self, argv, timeout_s, name):
        self.calls.append({"argv": argv, "timeout_s": timeout_s, "name": name})
        return self.runs.pop(0)


def raw(code=0, out=b"", cut=False, timed_out=False):
    return RawRun(returncode=code, output=out, output_cut=cut, timed_out=timed_out)


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    return d


def make(*runs, **kw):
    docker = FakeDocker(*runs)
    sandbox = Sandbox(docker=docker, profiles={"python-pytest": PROFILE}, **kw)
    return sandbox, docker


# --- the docker command ---------------------------------------------------------------

def test_command_is_fixed_and_locked_down(workdir):
    argv = build_command(PROFILE, workdir, "repopilot-abc")
    assert argv[:2] == ["docker", "run"]
    assert argv[-len(PROFILE.command):] == list(PROFILE.command)
    assert argv[-len(PROFILE.command) - 1] == "repopilot-pytest"
    joined = " ".join(argv)
    for flag in (
        "--rm", "--name repopilot-abc", "--network none", "--read-only", "--cap-drop ALL",
        "--security-opt no-new-privileges", "--memory 512m", "--memory-swap 512m",
        "--cpus 1", "--pids-limit 128", "--user 65534:65534", "-w /work",
        "PYTHONDONTWRITEBYTECODE=1",
    ):
        assert flag in joined, flag
    assert f"type=bind,source={workdir},target=/work,readonly" in joined
    assert "--tmpfs /tmp:rw,noexec,nosuid,size=64m" in joined
    assert "sh -c" not in joined and "--privileged" not in joined
    assert all(isinstance(a, str) for a in argv)


def test_no_host_environment_or_extra_mounts(workdir):
    argv = build_command(PROFILE, workdir, "n")
    assert argv.count("-v") == 0 and argv.count("--mount") == 1
    assert "--env-file" not in argv and "--network" in argv


@pytest.mark.parametrize("bad", ["a,b", "a:b"])
def test_workdir_with_odd_characters_rejected(tmp_path, bad):
    d = tmp_path / bad
    d.mkdir()
    sandbox, docker = make()
    with pytest.raises(SandboxError):
        sandbox.run_tests("python-pytest", d)
    assert docker.calls == []


def test_unknown_profile_rejected_before_docker(workdir):
    sandbox, docker = make()
    with pytest.raises(SandboxError, match="Unknown test profile"):
        sandbox.run_tests("rm -rf /", workdir)
    assert docker.calls == []


def test_missing_workdir_rejected(tmp_path):
    sandbox, docker = make()
    with pytest.raises(SandboxError):
        sandbox.run_tests("python-pytest", tmp_path / "nope")
    assert docker.calls == []


def test_run_uses_profile_timeout_and_a_unique_name(workdir):
    sandbox, docker = make(raw(0, b"1 passed"), raw(0, b"1 passed"))
    sandbox.run_tests("python-pytest", workdir)
    sandbox.run_tests("python-pytest", workdir)
    assert docker.calls[0]["timeout_s"] == 30
    assert docker.calls[0]["name"].startswith("repopilot-")
    assert docker.calls[0]["name"] != docker.calls[1]["name"]


# --- status mapping --------------------------------------------------------------------

@pytest.mark.parametrize(
    "code,status",
    [
        (0, ExecStatus.PASSED),
        (1, ExecStatus.FAILED),
        (2, ExecStatus.FAILED),  # collection error, e.g. the patch broke an import
        (5, ExecStatus.INCONCLUSIVE),  # no tests collected
        (3, ExecStatus.INCONCLUSIVE),
        (4, ExecStatus.INCONCLUSIVE),
        (137, ExecStatus.VIOLATION),  # killed: out of memory
        (126, ExecStatus.VIOLATION),
        (127, ExecStatus.VIOLATION),
        (99, ExecStatus.VIOLATION),
    ],
)
def test_exit_code_mapping(workdir, code, status):
    sandbox, _ = make(raw(code, b"output"))
    result = sandbox.run_tests("python-pytest", workdir)
    assert result.status is status
    assert result.exit_code == code
    if status is ExecStatus.VIOLATION:
        assert result.reason


def test_timeout_is_a_violation(workdir):
    sandbox, _ = make(raw(None, b"partial", timed_out=True))
    result = sandbox.run_tests("python-pytest", workdir)
    assert result.status is ExecStatus.VIOLATION
    assert "timeout" in result.reason.lower()
    assert result.exit_code is None


def test_docker_cannot_start_raises(workdir):
    sandbox, _ = make(raw(125, b"docker: Cannot connect to the Docker daemon at unix:///x"))
    with pytest.raises(SandboxError, match="Cannot connect"):
        sandbox.run_tests("python-pytest", workdir)


# --- logs and results -------------------------------------------------------------------

def test_log_keeps_the_tail_and_flags_truncation(workdir):
    out = (b"HEAD-" + b"x" * 50_000 + b"-TAIL")
    sandbox, _ = make(raw(1, out), max_log_chars=1000)
    result = sandbox.run_tests("python-pytest", workdir)
    assert len(result.log_tail) <= 1000
    assert result.log_tail.endswith("-TAIL")
    assert "HEAD-" not in result.log_tail
    assert result.truncated is True


def test_short_log_not_truncated(workdir):
    sandbox, _ = make(raw(0, b"1 passed in 0.01s"))
    result = sandbox.run_tests("python-pytest", workdir)
    assert result.log_tail == "1 passed in 0.01s" and result.truncated is False


def test_raw_output_cut_marks_truncated(workdir):
    sandbox, _ = make(raw(1, b"tail", cut=True))
    assert sandbox.run_tests("python-pytest", workdir).truncated is True


def test_invalid_utf8_in_log_does_not_crash(workdir):
    sandbox, _ = make(raw(1, b"bad \xff\xfe bytes"))
    assert "bad" in sandbox.run_tests("python-pytest", workdir).log_tail


def test_failed_test_names_are_parsed(workdir):
    out = (
        b"FAILED tests/test_cart.py::test_average_price_of_empty_cart_is_zero - "
        b"ZeroDivisionError: division by zero\n"
        b"ERROR tests/test_other.py - SyntaxError\n"
        b"1 failed, 1 passed in 0.03s\n"
    )
    sandbox, _ = make(raw(1, out))
    result = sandbox.run_tests("python-pytest", workdir)
    assert result.failed_tests == [
        "tests/test_cart.py::test_average_price_of_empty_cart_is_zero",
        "tests/test_other.py",
    ]


def test_parse_failed_tests_dedupes_and_caps():
    text = "\n".join(f"FAILED t.py::test_{i} - boom" for i in range(100))
    text += "\nFAILED t.py::test_1 - boom again"
    names = parse_failed_tests(text)
    assert len(names) == 50 and len(set(names)) == 50
    assert parse_failed_tests("FAILED " + "a" * 500)[0] == "a" * 200


def test_results_are_stored_and_retrievable(workdir):
    sandbox, _ = make(raw(0, b"1 passed"))
    result = sandbox.run_tests("python-pytest", workdir)
    assert sandbox.get_results(result.id) is result
    assert sandbox.get_results("nope") is None


# --- the real runner, against a stub `docker` script ------------------------------------

@pytest.fixture
def stub_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def write_stub(bin_dir, body):
    path = bin_dir / "docker"
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_runner_returns_output_and_exit_code(stub_bin):
    write_stub(stub_bin, 'echo "args: $@"; echo oops >&2; exit 1\n')
    run = SubprocessDocker().run(["docker", "run", "x"], timeout_s=5, name="n")
    assert run.returncode == 1 and run.timed_out is False
    assert b"args: run x" in run.output and b"oops" in run.output


def test_runner_force_removes_the_container_on_timeout(stub_bin, tmp_path):
    marker = tmp_path / "killed"
    write_stub(
        stub_bin,
        f'if [ "$1" = "rm" ]; then echo "$2 $3" > {marker}; exit 0; fi\nsleep 30\n',
    )
    started = time.monotonic()
    run = SubprocessDocker().run(["docker", "run", "x"], timeout_s=1, name="repopilot-zzz")
    assert time.monotonic() - started < 10
    assert run.timed_out is True
    assert marker.read_text().strip() == "-f repopilot-zzz"


def test_runner_bounds_memory_for_huge_output(stub_bin):
    write_stub(stub_bin, "head -c 3000000 /dev/zero | tr '\\0' x; echo; echo FINAL-LINE\n")
    run = SubprocessDocker(max_raw_bytes=100_000).run(["docker", "run"], timeout_s=20, name="n")
    assert len(run.output) <= 100_000
    assert run.output_cut is True
    assert run.output.rstrip().endswith(b"FINAL-LINE")


def test_runner_reports_missing_docker(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SandboxError, match="not installed"):
        SubprocessDocker().run(["docker", "run"], timeout_s=5, name="n")
