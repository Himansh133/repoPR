"""End to end with a real git clone and a real Docker container; only the model is scripted.

Run with: pytest -m docker
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent.fix import RunBudgets, RunStatus, run_issue
from app.report import build_report
from app.sandbox import Sandbox
from tests.fakes import FakeLLM, make_diff, plan_args, reply, tool_call
from tests.test_fix_run import BUG, CART, ISSUE, RIGHT, WRONG

pytestmark = pytest.mark.docker

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _image_ready():
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", "repopilot-pytest"], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture
def source(tmp_path):
    if not _image_ready():
        pytest.skip("Docker or the repopilot-pytest image is not available")
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=root, check=True, capture_output=True,
        )
    return root


def script(*diffs):
    steps = [
        reply(tool_call("read_file", {"path": "shop/cart.py"}, "r1")),
        reply(tool_call("submit_plan", plan_args(), "p1")),
    ]
    for i, diff in enumerate(diffs):
        steps.append(reply(tool_call("propose_patch", {"diff": diff}, f"pp{i}")))
    return FakeLLM(steps)


async def test_real_container_confirms_a_good_fix(source):
    llm = script(make_diff("shop/cart.py", CART, RIGHT))
    result = await run_issue(llm, source, ISSUE, Sandbox(), RunBudgets())
    try:
        assert result.baseline.failed_tests, result.baseline.log_tail
        assert result.status is RunStatus.PASSED, result.reason
        assert result.repairs_used == 0
        assert "Status: PASSED" in build_report(result, ISSUE)
        assert (source / "shop" / "cart.py").read_text() == CART  # original untouched
    finally:
        shutil.rmtree(result.workdir, ignore_errors=True)


async def test_real_container_catches_a_wrong_fix_then_accepts_the_repair(source):
    llm = script(make_diff("shop/cart.py", CART, WRONG), make_diff("shop/cart.py", WRONG, RIGHT))
    result = await run_issue(llm, source, ISSUE, Sandbox(), RunBudgets())
    try:
        assert result.status is RunStatus.PASSED, result.reason
        assert result.repairs_used == 1
        assert result.executions[0].status.value == "failed"
        assert BUG.strip() in result.final_diff
    finally:
        shutil.rmtree(result.workdir, ignore_errors=True)
