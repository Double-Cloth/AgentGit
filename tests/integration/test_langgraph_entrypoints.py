"""图 CLI 提交、现有 HTTP 查询/控制与 Worker 共用耐久身份。"""

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_langgraph_runs import archive as archive
from test_langgraph_runs import case, state
from test_mcp_adapter import server as server
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.services.management import ManagementService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_cli_submit_status_and_http_resume_are_exactly_bound(
    history, server, tmp_path, monkeypatch
):
    queue, _, repo, program, _, counts, _ = case(history, server, tmp_path, submit=False)
    service = ManagementService(queue.research, actor="graph-operator")
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    path = tmp_path / "graph-program.json"
    path.write_bytes(encode(program.model_dump(mode="json")))
    runner = CliRunner()
    args = [
        "langgraph-submit",
        str(repo),
        str(path),
        "--expected-version",
        "0",
        "--key",
        "cli-once",
        "--testing",
    ]
    submitted = runner.invoke(app, args)
    assert submitted.exit_code == 0, submitted.output
    receipt = json.loads(submitted.output)
    assert json.loads(runner.invoke(app, args).output) == receipt
    run = UUID(receipt["run_id"])
    assert Worker(service.jobs).run_once()["status"] == "paused"
    token = "fixture-langgraph-api-token-0000000000000000"
    with TestClient(
        create_app(
            Settings(api_token=SecretStr(token), operator_id="graph-operator"), management=service
        ),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(repo)},
    ) as client:
        data = client.get(f"/runs/{run}").json()
        assert data["adapter"]["kind"] == "langgraph_program/v1"
        assert data["adapter"]["native_next"] == ["review"]
        assert data["adapter"]["logical_usage"]["model_calls"] == 0
        before = counts()
        response = client.post(
            f"/runs/{run}/resume",
            json={
                "schema_version": 1,
                "expected_version": data["state_version"],
            },
            headers={"Idempotency-Key": "resume-once"},
        )
        assert response.status_code == 202, response.text
        assert Worker(service.jobs).run_once()["status"] == "completed"
        _, _, row = state(history, repo, run)
        for _ in range(2):
            for path in [
                f"/runs/{run}",
                f"/runs/{run}/events",
                f"/runs/{run}/artifacts",
                f"/commits/{row['checkpoint_commit_id']}",
            ]:
                response = client.get(path)
                assert response.status_code == 200, response.text
        other = history.create_repository("其他项目")
        assert (
            client.get(f"/runs/{run}", headers={"X-Repository-ID": str(other)}).status_code == 404
        )
        assert counts() == before
    status = runner.invoke(app, ["status", str(repo), str(run), "--testing"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["adapter"]["logical_usage"]["steps"] == 3
