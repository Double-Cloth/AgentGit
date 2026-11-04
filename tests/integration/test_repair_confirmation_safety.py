"""模型试图扩大工具权限时，验证拒绝且不会触发宿主/容器执行。"""

import pytest
from test_repair_confirmation import actual_worker, setup_confirmation

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import encode
from agentgit.core.models import parse_contract
from agentgit.core.research import ModelRequest, Plan, PlanDecision, PlanNode, ToolDecision
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.services.jobs import JobService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


class UnauthorisedExecutor(MockResearchModel):
    """受控恶意输出；未注册工具的 invoke 必须永远不可达。"""

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        if request.phase == "plan":
            value = PlanDecision(
                plan=Plan(nodes=(PlanNode(key="forbidden", kind="terminal_exec"),))
            )
        else:
            value = ToolDecision(
                node_key="forbidden", tool="terminal_exec", arguments={"schema_version": 1}
            )
        return encode(value.model_dump(mode="json"))


def test_confirmation_regression_never_grants_new_tool_permission(history, tmp_path, monkeypatch):
    repo, _, research, _, _ = setup_confirmation(history, tmp_path)
    runner, worker = (
        RepairConfirmationWorker(research),
        actual_worker(history, research, repo, "pass"),
    )
    for _ in range(6):
        runner.run_once(repository=repo)
        assert worker.run_once()["status"] == "completed"
    assert runner.run_once(repository=repo)["status"] == "regressing"
    calls = []
    original = ToolRegistry.invoke

    def guarded(self, name, arguments):
        if name in ("terminal_exec", "code_exec"):
            calls.append(name)
            pytest.fail("未授权执行器不应被调用")
        return original(self, name, arguments)

    monkeypatch.setattr(ToolRegistry, "invoke", guarded)
    result = Worker(JobService(research), model_factory=UnauthorisedExecutor).run_once()
    assert result["status"] == "failed"
    final = runner.run_once(repository=repo)
    assert final["status"] == "rejected" and final["stop_reason"] == "safety_violation"
    assert final["verification"]["confirm_passed"] == 6
    assert final["verification"]["regression_attempted"] == 1
    assert not final["verification"]["production_eligible"]
    assert final["trials"][-1]["result"]["category"] == "safety"
    assert final["trials"][-1]["result"]["safety_violations"] == ["forbidden"]
    assert final["trials"][-1]["result"]["usage"]["tool_calls"] == 0 and calls == []
