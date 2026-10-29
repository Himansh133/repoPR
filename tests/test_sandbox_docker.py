"""Integration tests: a real Docker container. Run with: pytest -m docker

Needs a running Docker daemon and the image built by ./docker/build.sh.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.patch import default_work_dir
from app.sandbox import ExecStatus, Sandbox, default_profiles

pytestmark = pytest.mark.docker

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _docker_ready():
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", "repopilot-pytest"], capture_output=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


@pytest.fixture(autouse=True)
def need_docker():
    if not _docker_ready():
        pytest.skip("Docker or the repopilot-pytest image is not available (run docker/build.sh)")


@pytest.fixture
def repo():
    # Under the home folder, not /tmp: Docker Desktop cannot mount /tmp.
    dest = Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=default_work_dir())) / "repo"
    shutil.copytree(FIXTURE, dest)
    yield dest
    shutil.rmtree(dest.parent, ignore_errors=True)


def short_timeout_sandbox(timeout_s, **kw):
    profile = default_profiles(timeout_s=timeout_s)["python-pytest"]
    return Sandbox(profiles={"python-pytest": profile}, **kw)


def add_test(repo, body):
    (repo / "tests" / "test_probe.py").write_text(body)


def test_fixture_bug_is_reported_by_test_name(repo):
    result = Sandbox().run_tests("python-pytest", repo)
    assert result.status is ExecStatus.FAILED
    assert result.exit_code == 1
    assert any("test_average_price_of_empty_cart_is_zero" in n for n in result.failed_tests)
    assert "ZeroDivisionError" in result.log_tail


def test_fixed_copy_passes(repo):
    cart = repo / "shop" / "cart.py"
    cart.write_text(
        cart.read_text().replace(
            "    return sum(prices) / len(prices)",
            "    if not prices:\n        return 0\n    return sum(prices) / len(prices)",
        )
    )
    result = Sandbox().run_tests("python-pytest", repo)
    assert result.status is ExecStatus.PASSED, result.log_tail
    assert result.failed_tests == []


def test_original_directory_is_not_modified(repo):
    before = sorted(p.name for p in repo.rglob("*"))
    Sandbox().run_tests("python-pytest", repo)
    assert sorted(p.name for p in repo.rglob("*")) == before  # no __pycache__, no .pytest_cache


def test_network_is_off(repo):
    add_test(
        repo,
        "import socket, pytest\n"
        "def test_no_network():\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('1.1.1.1', 53), timeout=3)\n",
    )
    (repo / "tests" / "test_cart.py").unlink()
    assert Sandbox().run_tests("python-pytest", repo).status is ExecStatus.PASSED


def test_repo_mount_is_read_only_and_user_is_not_root(repo):
    add_test(
        repo,
        "import os, pytest\n"
        "def test_ro_and_nonroot():\n"
        "    assert os.getuid() != 0\n"
        "    with pytest.raises(OSError):\n"
        "        open('/work/new_file.txt', 'w')\n"
        "    with pytest.raises(OSError):\n"
        "        open('/etc/new_file.txt', 'w')\n",
    )
    (repo / "tests" / "test_cart.py").unlink()
    result = Sandbox().run_tests("python-pytest", repo)
    assert result.status is ExecStatus.PASSED, result.log_tail


def test_timeout_kills_the_container(repo):
    add_test(repo, "import time\ndef test_slow():\n    time.sleep(60)\n")
    (repo / "tests" / "test_cart.py").unlink()
    result = short_timeout_sandbox(3).run_tests("python-pytest", repo)
    assert result.status is ExecStatus.VIOLATION
    assert "timeout" in result.reason
    left = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=repopilot-"], capture_output=True, text=True
    )
    assert left.stdout.strip() == ""


def test_huge_output_is_truncated(repo):
    add_test(
        repo,
        "def test_noisy():\n"
        "    print('x' * 1_000_000)\n"
        "    assert False, 'END-MARKER'\n",
    )
    (repo / "tests" / "test_cart.py").unlink()
    result = Sandbox().run_tests("python-pytest", repo)
    assert result.status is ExecStatus.FAILED
    assert len(result.log_tail) <= 20_000
    assert result.truncated is True
    assert "test_noisy" in result.failed_tests[0]


def test_out_of_memory_is_a_violation(repo):
    add_test(repo, "def test_hog():\n    x = bytearray(2_000_000_000)\n    assert x\n")
    (repo / "tests" / "test_cart.py").unlink()
    result = Sandbox().run_tests("python-pytest", repo)
    assert result.status is ExecStatus.VIOLATION, result.log_tail


def test_no_tests_is_inconclusive(repo):
    shutil.rmtree(repo / "tests")
    result = Sandbox().run_tests("python-pytest", repo)
    assert result.status is ExecStatus.INCONCLUSIVE
    assert result.exit_code == 5


def test_requirements_are_installed_outside_the_repo_and_seen_read_only(repo):
    (repo / "requirements.txt").write_text("six==1.16.0\n")
    add_test(repo, (
        "import os, pathlib, six\n"
        "def test_six_is_importable_and_read_only():\n"
        "    assert six.PY3\n"
        "    assert pathlib.Path(six.__file__).is_relative_to('/deps')\n"
        "    assert not os.access('/deps', os.W_OK)\n"
    ))
    cart = repo / "shop" / "cart.py"  # fix the fixture bug so -x reaches the probe
    cart.write_text(cart.read_text().replace(
        "    return sum(prices) / len(prices)",
        "    if not prices:\n        return 0\n    return sum(prices) / len(prices)",
    ))
    sandbox = Sandbox()
    assert sandbox.prepare("python-pytest", repo) is not None
    assert not (repo / "six.py").exists()
    result = sandbox.run_tests("python-pytest", repo)
    assert result.status is ExecStatus.PASSED, result.log_tail
    deps = [p for p in repo.parent.iterdir() if p.name.startswith("repopilot-deps-")]
    assert len(deps) == 1
    sandbox.release(repo)
    assert not deps[0].exists()


def test_a_package_without_a_wheel_fails_the_install_cleanly(repo):
    (repo / "requirements.txt").write_text("repopilot-no-such-package-xyz==9.9.9\n")
    sandbox = Sandbox()
    with pytest.raises(Exception, match="dependency install failed"):
        sandbox.prepare("python-pytest", repo)
    assert not [p for p in repo.parent.iterdir() if p.name.startswith("repopilot-deps-")]
