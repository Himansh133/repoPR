"""Unit tests for app.sandbox. No real Docker: a fake runner, plus a stub `docker` script."""

import os
import stat
import time
from pathlib import Path

import pytest

from app.sandbox import (
    ExecStatus,
    RawRun,
    Sandbox,
    SandboxError,
    SandboxProfile,
    SubprocessDocker,
    build_command,
    build_install_command,
    default_profiles,
    detect_profile,
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


# --- node-npm profile -------------------------------------------------------------

NODE = default_profiles()["node-npm"]


def make_node(*runs):
    docker = FakeDocker(*runs)
    return Sandbox(docker=docker, profiles={"node-npm": NODE}), docker


def test_detect_profile(tmp_path):
    assert detect_profile(tmp_path) == "python-pytest"
    (tmp_path / "package.json").write_text("{}")
    assert detect_profile(tmp_path) == "node-npm"
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_profile(tmp_path) == "python-pytest"


def test_node_test_run_is_offline_and_read_only(workdir):
    argv = build_command(NODE, workdir, "n")
    joined = " ".join(argv)
    assert "--network none" in joined and "--read-only" in joined
    assert f"type=bind,source={workdir},target=/work,readonly" in joined
    assert "CI=true" in argv and "NO_COLOR=1" in argv
    assert "/work/node_modules/.vite:rw,noexec,nosuid,mode=1777,size=64m" in argv
    assert argv[-2:] == ["npm", "test"]


def test_install_has_network_but_no_package_scripts(workdir):
    argv = build_install_command(NODE, workdir, "n", "1000:1000")
    joined = " ".join(argv)
    assert "--network" not in argv
    assert f"type=bind,source={workdir},target=/work" in joined and "readonly" not in joined
    for flag in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--user 1000:1000", "--memory 2g"):
        assert flag in joined, flag
    assert argv[-5:] == ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    assert "sh -c" not in joined


def test_prepare_runs_install_once(workdir):
    (workdir / "package-lock.json").write_text("{}")
    sandbox, docker = make_node(raw(0))
    assert sandbox.prepare("node-npm", workdir) is not None
    assert docker.calls[0]["argv"][-5:-3] == ["npm", "ci"]
    assert (workdir / "node_modules" / ".vite").is_dir()
    assert docker.calls[0]["timeout_s"] == NODE.install_timeout_s


def test_prepare_refuses_a_node_modules_symlink_out_of_the_repo(workdir, tmp_path):
    (workdir / "package-lock.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workdir / "node_modules").symlink_to(outside)
    sandbox, _ = make_node(raw(0))
    with pytest.raises(SandboxError, match="outside the repository"):
        sandbox.prepare("node-npm", workdir)
    assert list(outside.iterdir()) == []


def test_prepare_needs_a_lockfile(workdir):
    sandbox, docker = make_node()
    with pytest.raises(SandboxError, match="package-lock.json"):
        sandbox.prepare("node-npm", workdir)
    assert docker.calls == []


def test_prepare_failure_and_timeout_raise(workdir):
    (workdir / "package-lock.json").write_text("{}")
    sandbox, _ = make_node(raw(1, b"npm ERR! 404 Not Found"), raw(timed_out=True, code=None))
    with pytest.raises(SandboxError, match="install failed .*404"):
        sandbox.prepare("node-npm", workdir)
    with pytest.raises(SandboxError, match="time limit"):
        sandbox.prepare("node-npm", workdir)


def test_prepare_is_a_no_op_without_install(workdir):
    sandbox, docker = make(raw(0))
    assert sandbox.prepare("python-pytest", workdir) is None
    assert docker.calls == []


@pytest.mark.parametrize(
    "code,out,status",
    [
        (0, b"# pass 3", ExecStatus.PASSED),
        (1, b"not ok 1 - adds", ExecStatus.FAILED),
        (2, b"something odd", ExecStatus.FAILED),
        (5, b"", ExecStatus.FAILED),
        (1, b'npm error Missing script: "test"', ExecStatus.INCONCLUSIVE),
        (1, b"Error: no test specified", ExecStatus.INCONCLUSIVE),
        (137, b"", ExecStatus.VIOLATION),
    ],
)
def test_npm_exit_codes(workdir, code, out, status):
    sandbox, _ = make_node(raw(code, out))
    assert sandbox.run_tests("node-npm", workdir).status is status


def test_node_failed_test_names():
    log = (
        "not ok 1 - total adds tax\n"
        "  ✖ shipping is free over 50 (1.2ms)\n"
        " FAIL  src/cart.test.ts > cart > empty\n"
        "  ● checkout › applies coupons\n"
        "ok 2 - unrelated\n"
    )
    assert parse_failed_tests(log, "node-npm") == [
        "total adds tax", "shipping is free over 50", "src/cart.test.ts > cart > empty",
        "checkout › applies coupons",
    ]
    assert parse_failed_tests(log) == []


# --- python dependencies ------------------------------------------------------------

PY = default_profiles()["python-pytest"]


def make_py(*runs):
    docker = FakeDocker(*runs)
    return Sandbox(docker=docker, profiles={"python-pytest": PY}), docker


def mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]


def test_python_prepare_is_a_no_op_without_requirements(workdir):
    sandbox, docker = make_py()
    assert sandbox.prepare("python-pytest", workdir) is None
    assert docker.calls == []


def test_python_install_uses_wheels_only_into_a_folder_outside_the_repo(workdir):
    (workdir / "requirements.txt").write_text("# deps\nrequests==2.32.3\n\nattrs>=23\n")
    sandbox, docker = make_py(raw(0), raw(0, b"1 passed"))
    assert sandbox.prepare("python-pytest", workdir) is not None
    argv = docker.calls[0]["argv"]
    assert argv[argv.index("repopilot-pytest") + 1:] == [
        "python", "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
        "--no-input", "--only-binary=:all:", "--target", "/deps", "-r", "requirements.txt",
    ]
    repo_mount, deps_mount = mounts(argv)
    assert repo_mount == f"type=bind,source={workdir},target=/work,readonly"
    deps = deps_mount.split("source=")[1].split(",")[0]
    assert deps_mount == f"type=bind,source={deps},target=/deps"
    assert os.path.isdir(deps) and not Path(deps).is_relative_to(workdir)
    assert "--network" not in argv and "--read-only" in argv

    sandbox.run_tests("python-pytest", workdir)
    test_argv = docker.calls[1]["argv"]
    assert f"type=bind,source={deps},target=/deps,readonly" in mounts(test_argv)
    assert "PYTHONPATH=/deps" in test_argv
    assert test_argv[test_argv.index("--network") + 1] == "none"


@pytest.mark.parametrize("line", [
    "-e .", "--no-binary :all:", "-r other.txt", "--index-url https://evil.example/simple",
    "./local_pkg", "/abs/pkg", "pkg @ https://example.com/pkg.whl", "git+https://x/y.git",
])
def test_python_install_refuses_options_paths_and_urls(workdir, line):
    (workdir / "requirements.txt").write_text(f"requests==2.32.3\n{line}\n")
    sandbox, docker = make_py()
    with pytest.raises(SandboxError, match="requirements.txt line 2"):
        sandbox.prepare("python-pytest", workdir)
    assert docker.calls == []


def test_python_install_failure_removes_the_deps_folder(workdir):
    (workdir / "requirements.txt").write_text("nosuchpkg==1\n")
    sandbox, docker = make_py(raw(1, b"ERROR: No matching distribution found for nosuchpkg==1"))
    with pytest.raises(SandboxError, match="No matching distribution"):
        sandbox.prepare("python-pytest", workdir)
    deps = mounts(docker.calls[0]["argv"])[1].split("source=")[1].split(",")[0]
    assert not os.path.exists(deps)


def test_release_removes_the_deps_folder(workdir):
    (workdir / "requirements.txt").write_text("attrs==23.2.0\n")
    sandbox, docker = make_py(raw(0), raw(0))
    sandbox.prepare("python-pytest", workdir)
    deps = mounts(docker.calls[0]["argv"])[1].split("source=")[1].split(",")[0]
    sandbox.release(workdir)
    sandbox.release(workdir)  # safe to call twice
    assert not os.path.exists(deps)
    sandbox.run_tests("python-pytest", workdir)
    argv = docker.calls[1]["argv"]
    assert len(mounts(argv)) == 1 and "PYTHONPATH=/deps" not in argv


def test_needs_reinstall_only_for_the_profiles_install_files():
    sandbox = Sandbox(docker=FakeDocker(), profiles=default_profiles())
    assert sandbox.needs_reinstall("python-pytest", ["shop/a.py", "requirements.txt"])
    assert not sandbox.needs_reinstall("python-pytest", ["shop/a.py", "docs/requirements.txt"])
    assert sandbox.needs_reinstall("node-npm", ["package-lock.json"])
    assert not sandbox.needs_reinstall("node-npm", ["src/index.js"])
    assert not sandbox.needs_reinstall("nope", ["requirements.txt"])


def test_a_second_python_install_replaces_the_first_folder(workdir):
    (workdir / "requirements.txt").write_text("attrs==23.2.0\n")
    sandbox, docker = make_py(raw(0), raw(0), raw(0))
    sandbox.prepare("python-pytest", workdir)
    first = mounts(docker.calls[0]["argv"])[1].split("source=")[1].split(",")[0]
    sandbox.prepare("python-pytest", workdir)
    second = mounts(docker.calls[1]["argv"])[1].split("source=")[1].split(",")[0]
    assert first != second and not os.path.exists(first) and os.path.isdir(second)
    sandbox.run_tests("python-pytest", workdir)
    assert f"type=bind,source={second},target=/deps,readonly" in mounts(docker.calls[2]["argv"])
    sandbox.release(workdir)


def test_removing_requirements_drops_the_installed_packages(workdir):
    (workdir / "requirements.txt").write_text("attrs==23.2.0\n")
    sandbox, docker = make_py(raw(0), raw(0))
    sandbox.prepare("python-pytest", workdir)
    deps = mounts(docker.calls[0]["argv"])[1].split("source=")[1].split(",")[0]
    (workdir / "requirements.txt").unlink()
    assert sandbox.prepare("python-pytest", workdir) is None
    assert not os.path.exists(deps)
    sandbox.run_tests("python-pytest", workdir)
    assert "PYTHONPATH=/deps" not in docker.calls[1]["argv"]
