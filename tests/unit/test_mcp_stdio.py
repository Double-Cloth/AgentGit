"""真实可信 Python 子进程的启动边界、有限帧与清理，无数据库依赖。"""

import json
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.adapters.mcp import MCPAdapter, MCPClient
from agentgit.adapters.mcp_http import MCPTransportFailure
from agentgit.adapters.mcp_stdio import post
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import MCPArguments, MCPStdioServer
from agentgit.core.mcp_stdio import MCPApprovedProgram, MCPStdioRegistry
from agentgit.core.models import EffectClass


def test_approved_real_stdio_is_isolated_bounded_and_cleaned(tmp_path, monkeypatch, mcp_stdio):
    server, _, _, rows, exited = mcp_stdio
    monkeypatch.setenv("AGENTGIT_API_TOKEN", "fixture-parent-private-93af")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "forbidden-imports"))
    client = MCPClient(server)
    binding = client.bind("echo", EffectClass.READ_ONLY)
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/mcp/v1/tools.json").read_text("utf-8")
    )
    assert json.loads(binding.definition_json) == fixture
    result = json.loads(
        MCPAdapter(binding, client).invoke(
            encode(MCPArguments(arguments={"value": "stdio-value"}).model_dump(mode="json"))
        )
    )
    assert result["structuredContent"] == {"value": "stdio-value"}
    records = rows()
    starts = [r for r in records if r["event"] == "started"]
    assert len(starts) == 3 and len([r for r in records if r["event"] == "effect"]) == 1
    for row in starts:
        assert row["isolated"] == row["no_site"] == 1 and row["dont_write_bytecode"]
        assert "AGENTGIT_API_TOKEN" not in row["env_keys"] and "PYTHONPATH" not in row["env_keys"]
        assert not Path(row["cwd"]).exists() and exited(row["pid"])
    assert [r["method"] for r in records if r["event"] == "request"] == [
        "server/discover",
        "tools/list",
        "server/discover",
        "tools/list",
        "server/discover",
        "tools/call",
    ]


@pytest.mark.parametrize("mode", ["timeout", "drop", "oversize", "wrong_id", "tool_error"])
def test_uncertain_stdio_write_is_never_success_and_processes_exit(mcp_stdio, mode):
    server, _, control, rows, exited = mcp_stdio
    client = MCPClient(server)
    binding = client.bind("echo", EffectClass.NON_IDEMPOTENT_WRITE)
    control.write_text(mode, encoding="utf-8")
    before = time.monotonic()
    with pytest.raises(AgentGitError) as caught:
        MCPAdapter(binding, client).invoke(
            encode(MCPArguments(arguments={"value": "write-once"}).model_dump(mode="json"))
        )
    assert caught.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert time.monotonic() - before < 5
    assert len([r for r in rows() if r["event"] == "effect"]) == 1
    assert all(exited(r["pid"]) for r in rows() if r["event"] == "started")


@pytest.mark.parametrize("changed", ["script", "interpreter", "unregistered"])
def test_changed_or_unregistered_program_does_not_start(mcp_stdio, changed):
    server, program, _, rows, _ = mcp_stdio
    if changed == "script":
        script = Path(program.script)
        script.write_bytes(script.read_bytes() + b"\n# changed\n")
    elif changed == "interpreter":
        program = program.model_copy(update={"interpreter_sha256": "0" * 64})
        Path(os.environ["AGENTGIT_MCP_STDIO_REGISTRY"]).write_bytes(
            encode(MCPStdioRegistry(programs=(program,)).model_dump(mode="json"))
        )
        server = server.model_copy(update={"program_ref": program.reference()})
    else:
        server = server.model_copy(update={"program_ref": "0" * 64})
    with pytest.raises(AgentGitError) as caught:
        MCPClient(server).bind("echo", EffectClass.READ_ONLY)
    assert caught.value.code == ErrorCode.FORBIDDEN and rows() == []


def test_missing_approval_registry_and_extra_launch_fields_are_rejected(monkeypatch, mcp_stdio):
    server, program, _, rows, _ = mcp_stdio
    monkeypatch.delenv("AGENTGIT_MCP_STDIO_REGISTRY")
    with pytest.raises(AgentGitError) as caught:
        MCPClient(server).list_tools()
    assert caught.value.code == ErrorCode.FORBIDDEN and rows() == []
    with pytest.raises(ValidationError):
        MCPStdioServer.model_validate({**server.model_dump(), "command": "cmd.exe"})
    with pytest.raises(ValidationError):
        MCPApprovedProgram.model_validate({**program.model_dump(), "args": ["-c", "print(1)"]})
    with pytest.raises(AgentGitError):
        MCPClient(server, credential_env="AGENTGIT_API_TOKEN")


@pytest.mark.parametrize("mode", ["legacy", "stall_discovery"])
def test_discovery_fails_without_tool_call_or_restart(mcp_stdio, mode):
    server, _, control, rows, exited = mcp_stdio
    control.write_text(mode, encoding="utf-8")
    with pytest.raises(AgentGitError):
        MCPClient(server).bind("echo", EffectClass.READ_ONLY)
    assert [r["method"] for r in rows() if r["event"] == "request"] == ["server/discover"]
    assert all(exited(r["pid"]) for r in rows() if r["event"] == "started")


def test_already_expired_deadline_never_starts_server(mcp_stdio):
    server, _, _, rows, _ = mcp_stdio
    with pytest.raises(MCPTransportFailure) as caught:
        post(server, b"{}", deadline=0)
    assert not caught.value.sent and rows() == []


def test_owned_server_cannot_leave_child_process(mcp_stdio):
    server, _, control, rows, exited = mcp_stdio
    client = MCPClient(server)
    binding = client.bind("echo", EffectClass.READ_ONLY)
    control.write_text("child", encoding="utf-8")
    result = MCPAdapter(binding, client).invoke(
        encode(MCPArguments(arguments={"value": "child-boundary"}).model_dump(mode="json"))
    )
    assert json.loads(result)["structuredContent"] == {"value": "child-boundary"}
    events = rows()
    if os.name == "nt":
        assert any(r["event"] == "child_blocked" for r in events)
        assert not any(r["event"] == "child_allowed" for r in events)
    else:
        child_pids = [r["child_pid"] for r in events if r["event"] == "child_allowed"]
        assert len(child_pids) == 1
        # SIGKILL 异步生效，等待 Server 本身退出并不等于其子进程已完成退出。
        deadline = time.monotonic() + 2
        running = [pid for pid in child_pids if not exited(pid)]
        while running and time.monotonic() < deadline:
            time.sleep(0.01)
            running = [pid for pid in running if not exited(pid)]
        assert not running, f"MCP 子进程未在清理后退出 {running}"
    assert all(exited(r["pid"]) for r in events if r["event"] == "started")
