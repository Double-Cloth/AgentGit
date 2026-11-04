"""受信 stdio 上复用固定图全契约；进程、登记和协议帧另有原始证据。"""

import json
import os
import socket
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
import test_langgraph_forks as fork_cases
import test_langgraph_recorded as recorded_cases
import test_langgraph_runs as live_cases
from test_langgraph_runs import archive as archive
from test_mcp_stdio_runs import stdio_server_case

from agentgit.adapters.mcp import MCPClient
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def server(mcp_stdio):
    value = stdio_server_case(mcp_stdio)
    yield value
    assert value[1]()["all_exited"], "每次协议请求的自有 stdio 进程必须完成清理"


# 原函数的参数化标记及全部断言保留，传输由本模块的 server fixture 明确替换。
for module in (live_cases, fork_cases, recorded_cases):
    for original in sorted(name for name in vars(module) if name.startswith("test_")):
        name = "test_stdio_" + original.removeprefix("test_")
        assert name not in globals()
        globals()[name] = getattr(module, original)


def test_stdio_graph_recorded_survives_removed_program_and_registry(
    history, server, mcp_stdio, tmp_path, monkeypatch
):
    queue, service, repo, source, final, last, counts = recorded_cases.finished(
        history, server, tmp_path
    )
    first = recorded_cases.checkpoints(history, repo, source)[0][0]
    replay = service.recorded(
        repo,
        recorded_cases.request(source, first, last),
        actor="graph-operator",
        key="no-stdio-runtime",
    )
    script = Path(mcp_stdio[1].script)
    registry = Path(os.environ["AGENTGIT_MCP_STDIO_REGISTRY"])
    assert script.resolve().is_relative_to(
        tmp_path.resolve()
    ) and registry.resolve().is_relative_to(tmp_path.resolve())
    original_script, original_registry = script.read_bytes(), registry.read_bytes()
    before = counts()
    attempts = []

    def no_socket(*args, **kwargs):
        attempts.append(str(args[1:]))
        raise AssertionError("Recorded 不能连接网络")

    script.unlink()
    registry.unlink()
    try:
        assert not script.exists() and not registry.exists()
        with monkeypatch.context() as offline:
            offline.setattr(subprocess, "Popen", live_cases.forbidden)
            offline.setattr(socket.socket, "connect", no_socket)
            result = Worker(
                queue, mcp_client_factory=live_cases.forbidden, model_factory=live_cases.forbidden
            ).run_once()
        assert result["status"] == "completed", result
        current, _, _ = live_cases.state(history, repo, UUID(replay.run_id))
        assert (
            current.logical_projection() == final.logical_projection()
            and current.actual_attempts == 0
        )
        assert counts() == before and attempts == []
        (tmp_path / "stdio-runtime-unavailable.json").write_text(
            json.dumps(
                {
                    "source_run_id": str(source),
                    "recorded_run_id": replay.run_id,
                    "script_absent": not script.exists(),
                    "registry_absent": not registry.exists(),
                    "before_counts": before,
                    "after_counts": counts(),
                    "socket_attempts": attempts,
                    "source_projection": final.logical_projection(),
                    "recorded_projection": current.logical_projection(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        script.write_bytes(original_script)
        registry.write_bytes(original_registry)


def test_stdio_graph_lost_lease_between_discovery_and_tool_never_sends_fetch(
    history, server, mcp_stdio, tmp_path
):
    queue, _, repo, _, receipt, counts, _ = live_cases.case(history, server, tmp_path)
    injected = []

    class FencedClient(MCPClient):
        def guard_requests(self, guard):
            def fenced():
                requests = [r for r in mcp_stdio[3]() if r["event"] == "request"]
                if (
                    not injected
                    and requests[-1]["method"] == "server/discover"
                    and sum(r["method"] == "tools/list" for r in requests) == 2
                ):
                    with history.engine.begin() as connection:
                        jobs = TABLES["jobs"]
                        connection.execute(
                            jobs.update()
                            .where(jobs.c.id == UUID(receipt.job_id))
                            .values(fencing_token=jobs.c.fencing_token + 1, worker="other-worker")
                        )
                    injected.append(requests[-1]["pid"])
                guard()

            super().guard_requests(fenced)

    result = Worker(
        queue, mcp_client_factory=lambda binding: FencedClient(binding.server)
    ).run_once()
    assert result["status"] == "lease_lost" and len(injected) == 1
    assert counts()["calls"] == 0 and counts()["requests"] == 5 and counts()["processes"] == 3
    assert counts()["all_exited"]
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(receipt.job_id))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    before = counts()
    result = Worker(queue, mcp_client_factory=live_cases.forbidden).run_once()
    assert result["status"] == "outcome_unknown", result
    assert counts() == before


def test_stdio_graph_changed_approved_script_is_rejected_before_process(
    history, server, mcp_stdio, tmp_path
):
    queue, _, repo, _, receipt, counts, _ = live_cases.case(history, server, tmp_path)
    script = Path(mcp_stdio[1].script)
    assert script.resolve().is_relative_to(tmp_path.resolve())
    original = script.read_bytes()
    before = counts()
    script.write_bytes(original + "\n# 测试注入：不再匹配批准摘要\n".encode())
    try:
        result = Worker(queue).run_once()
        assert result["status"] == "failed", result
        assert counts() == before and counts()["calls"] == 0
    finally:
        script.write_bytes(original)
