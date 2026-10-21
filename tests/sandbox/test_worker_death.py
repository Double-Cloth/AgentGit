"""实际杀死宿主测试 Worker 后，容器仍由自己的 PID 1 到期终止。"""

import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import EngineError

pytestmark = pytest.mark.sandbox


def test_deadline_survives_worker_process_death(engine, image_id):
    identity = ExecutionIdentity(str(uuid4()), str(uuid4()), 0, 1)
    container = SandboxContainer(engine, image_id, identity, 7000)
    helper = Path(__file__).resolve().parents[1] / "helpers/sandbox_worker.py"
    process = subprocess.Popen(
        [sys.executable, str(helper), image_id, identity.run_id, identity.call_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            assert process.poll() is None, "测试 Worker 未进入真实执行"
            try:
                state = engine.request("GET", f"/containers/{identity.name}/json")
                container.container_id = state["Id"]
                top = engine.request("GET", f"/containers/{container.container_id}/top")
                if any(row[0] == "10002" for row in top["Processes"]):
                    break
            except EngineError as error:
                assert error.status in {404, 409}
            time.sleep(0.05)
        else:
            pytest.fail("执行身份的子进程未出现")
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 9
        while time.monotonic() < deadline:
            state = container.inspect()["State"]
            if not state["Running"]:
                assert state["ExitCode"] == 124
                break
            time.sleep(0.1)
        else:
            pytest.fail("Worker 死亡后 PID 1 未按独立截止时间停止容器")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        assert container.cleanup()
