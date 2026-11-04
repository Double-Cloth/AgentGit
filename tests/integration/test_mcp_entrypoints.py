"""MCP 操作者 CLI、既有 HTTP 审批/查询和 Recorded 使用同一任务。"""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
import test_mcp_adapter as http_cases
from fastapi.testclient import TestClient
from mcp_evidence import archive_mcp_run as archive_mcp_run
from pydantic import SecretStr
from test_mcp_runs import isolated_worker_credentials as isolated_worker_credentials
from test_mcp_runs import state
from test_mcp_stdio_runs import stdio_server_case
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.mcp import MCPArguments, MCPBinding, MCPStdioServer
from agentgit.core.mcp_runs import MCPProgram, MCPRecordedSpec
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.versions import NewBranch
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
http_server = http_cases.server


@pytest.fixture(params=["http", "stdio"])
def server(request, http_server, mcp_stdio):
    return http_server if request.param == "http" else stdio_server_case(mcp_stdio)


def test_cli_http_approval_query_and_recorded_share_one_real_run(
    history, server, tmp_path, monkeypatch
):
    config, counts, _ = server
    service = ManagementService(ResearchService(history, tmp_path / "data"), actor="mcp-operator")
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    repo = history.create_repository("MCP 入口")
    runner = CliRunner()
    bound = runner.invoke(
        app,
        [
            "mcp-stdio-bind" if isinstance(config, MCPStdioServer) else "mcp-bind",
            config.server_identity,
            config.program_ref if isinstance(config, MCPStdioServer) else config.endpoint,
            "echo",
            "--effect",
            "NON_IDEMPOTENT_WRITE",
            "--testing",
        ],
    )
    assert bound.exit_code == 0, bound.output
    binding = parse_contract(encode(json.loads(bound.output)), MCPBinding)
    program = MCPProgram(binding=binding, request=MCPArguments(arguments={"value": "cli-value"}))
    program_file = tmp_path / "program.json"
    program_file.write_bytes(encode(program.model_dump(mode="json")))
    args = [
        "mcp-submit",
        str(repo),
        str(program_file),
        "--expected-version",
        "0",
        "--key",
        "cli-live",
        "--testing",
    ]
    submitted = runner.invoke(app, args)
    assert submitted.exit_code == 0, submitted.output
    receipt = json.loads(submitted.output)
    assert json.loads(runner.invoke(app, args).output) == receipt
    run = UUID(receipt["run_id"])
    assert (
        Worker(service.jobs).run_once()["status"] == "waiting_approval" and counts()["calls"] == 0
    )
    token = "fixture-mcp-api-token-000000000000000000000"
    with TestClient(
        create_app(
            Settings(api_token=SecretStr(token), operator_id="mcp-operator"), management=service
        ),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(repo)},
    ) as client:
        view = client.get(f"/runs/{run}")
        assert view.status_code == 200, view.text
        data = view.json()
        assert data["adapter"]["kind"] == "mcp_single_call/v1" and not data["adapter"]["has_model"]
        assert data["adapter"]["effect_class"] == "NON_IDEMPOTENT_WRITE"
        page = client.get("/approvals", params={"run_id": str(run)})
        assert page.status_code == 200, page.text
        pending = page.json()["items"][0]
        assert pending["request"]["arguments"] == {"value": "cli-value"}
        assert pending["version"] == data["state_version"]
        decision = client.post(
            f"/approvals/{pending['id']}/decision",
            json={
                "schema_version": 1,
                "decision": "approve",
                "expected_version": pending["version"],
            },
            headers={"Idempotency-Key": "mcp-approve"},
        )
        assert decision.status_code == 202, decision.text
        assert Worker(service.jobs).run_once()["status"] == "completed" and counts()["calls"] == 1
        before = counts()
        original, _, source_row = state(history, repo, run)
        for _ in range(2):
            for path in [
                f"/repositories/{repo}/runs",
                "/metrics",
                f"/runs/{run}",
                f"/runs/{run}/events",
                f"/runs/{run}/artifacts",
                f"/commits/{source_row['checkpoint_commit_id']}",
            ]:
                queried = client.get(path)
                assert queried.status_code == 200, queried.text
        other = history.create_repository("MCP 入口另一个项目")
        assert (
            client.get(f"/runs/{run}", headers={"X-Repository-ID": str(other)}).status_code == 404
        )
        assert counts() == before
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    status = runner.invoke(app, ["status", str(repo), str(run), "--testing"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["adapter"]["recording"]["from_commit"] == intent.checkpoint
    branch_file = tmp_path / "branch.json"
    branch_file.write_bytes(
        encode(
            NewBranch(
                source_run_id=str(run),
                from_commit=intent.checkpoint,
                name="cli-recorded",
                expected_version=0,
            ).model_dump(mode="json")
        )
    )
    forked = runner.invoke(app, ["mcp-fork", str(repo), str(branch_file), "--testing"])
    assert forked.exit_code == 0, forked.output
    replay_file = tmp_path / "replay.json"
    replay_file.write_bytes(
        encode(
            MCPRecordedSpec(
                source_run_id=str(run),
                from_commit=intent.checkpoint,
                to_commit=source_row["checkpoint_commit_id"],
            ).model_dump(mode="json")
        )
    )
    recorded = runner.invoke(
        app,
        [
            "mcp-submit",
            str(repo),
            str(program_file),
            "--expected-version",
            "0",
            "--key",
            "cli-recorded",
            "--branch",
            "cli-recorded",
            "--head",
            intent.checkpoint,
            "--replay-file",
            str(replay_file),
            "--testing",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    before = counts()
    if isinstance(config, MCPStdioServer):
        # 来源成功后移除启动白名单；纯 Recorded 仍不读取或解析它。
        monkeypatch.delenv("AGENTGIT_MCP_STDIO_REGISTRY")
    result = Worker(
        service.jobs, mcp_client_factory=lambda _: pytest.fail("Recorded 不能构造客户端")
    ).run_once()
    assert result["status"] == "completed" and counts() == before
    target = UUID(json.loads(recorded.output)["run_id"])
    assert state(history, repo, target)[0].logical_projection() == original.logical_projection()
    assert state(history, repo, target)[0].actual_attempts == 0
    queried = service.run(repo, target)
    assert queried["adapter"]["actual_attempts"] == 0
    assert queried["adapter"]["logical_usage"]["tool_calls"] == 1
    rejected = runner.invoke(
        app,
        [
            "mcp-bind",
            "private",
            "http://10.0.0.1:1234/mcp",
            "echo",
            "--effect",
            "READ_ONLY",
            "--testing",
        ],
    )
    assert rejected.exit_code == 1 and json.loads(rejected.output)["error"]["code"] == "validation"
    assert counts() == before
