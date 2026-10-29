"""MCP 有界任务的预算和逻辑状态不能伪装成模型或多次执行。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.adapters.mcp import MCPClient
from agentgit.core.canonical import encode
from agentgit.core.mcp import MCPArguments, MCPBinding, MCPServer
from agentgit.core.mcp_runs import MCPProgram, MCPRunState
from agentgit.core.models import EffectClass, RunStatus
from agentgit.core.research import BudgetLimits, BudgetUsage


def program():
    tool = json.loads((Path(__file__).parents[1] / "fixtures/mcp/v1/tools.json").read_text("utf-8"))
    binding = MCPBinding(
        server=MCPServer(server_identity="fixture", endpoint="http://127.0.0.1:1234/mcp"),
        tool_name="echo",
        definition_json=encode(tool).decode(),
        effect_class=EffectClass.READ_ONLY,
    )
    return MCPProgram(binding=binding, request=MCPArguments(arguments={"value": "ok"}))


@pytest.mark.parametrize(
    "change",
    [
        {"max_tool_calls": 0},
        {"max_tool_calls": 2},
        {"max_response_bytes": 128},
        {"max_artifact_bytes": 128},
        {"max_wall_ms": 1},
    ],
)
def test_program_cannot_exceed_its_fixed_budget(change):
    original = program()
    limits = BudgetLimits.model_validate({**original.limits.model_dump(), **change})
    with pytest.raises(ValidationError):
        MCPProgram(binding=original.binding, request=original.request, limits=limits)


@pytest.mark.parametrize(
    "change",
    [
        {"usage": BudgetUsage(model_calls=1)},
        {"usage": BudgetUsage(tool_calls=2, steps=2)},
        {"usage": BudgetUsage(tokens=1)},
        {"usage": BudgetUsage(cost_micro_units=1)},
        {"pending": True},
        {"actual_attempts": 1},
        {"status": RunStatus.COMPLETED},
    ],
)
def test_state_cannot_invent_model_usage_or_completion(change):
    with pytest.raises(ValidationError):
        MCPRunState(program_ref="a" * 64, **change)


def test_client_deadline_only_becomes_earlier(monkeypatch):
    client = MCPClient(program().binding.server)
    monkeypatch.setattr("agentgit.adapters.mcp.time.monotonic", lambda: 10.0)
    client.restrict_deadline(100)
    assert client._deadline == 10.1
    client.restrict_deadline(1000)
    assert client._deadline == 10.1
    client.restrict_deadline(20)
    assert client._deadline == 10.02
