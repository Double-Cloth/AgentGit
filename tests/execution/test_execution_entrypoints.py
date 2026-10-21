"""HTTP/CLI 配置入口、重复提交及真实五工具产物下载。"""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from agentgit.adapters.execution.policy import bundled_policy
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import AgentConfig
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("entrypoint", ["http", "cli"])
def test_five_tools_entrypoints_share_jobs_idempotency_and_artifacts(
    history, tmp_path, image_id, monkeypatch, entrypoint
):
    service = ManagementService(ResearchService(history, tmp_path / "entrypoints"))
    token = "fixture-execution-api-token-0000000000000000"
    settings = Settings(api_token=SecretStr(token))
    with TestClient(
        create_app(settings, management=service),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        response = client.post(
            "/repositories",
            json={"schema_version": 1, "name": "容器入口测试", "expected_version": 0},
            headers={"Idempotency-Key": "repository"},
        )
        assert response.status_code == 201, response.text
        repo = response.json()["repository_id"]
        client.headers["X-Repository-ID"] = repo
        config = AgentConfig(
            tools=(*AgentConfig().tools, "code_exec", "terminal_exec"),
            execution_policy=bundled_policy(),
        )
        if entrypoint == "cli":
            monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kw: service)
            args = [
                "config",
                repo,
                "--expected-version",
                "0",
                "--with-execution",
                "--key",
                "config",
            ]
            configured = CliRunner().invoke(app, args)
            assert configured.exit_code == 0, configured.stdout
            config_id = json.loads(configured.stdout)["config_id"]
            repeated = CliRunner().invoke(app, args)
            assert repeated.exit_code == 0
            assert json.loads(repeated.stdout) == json.loads(configured.stdout)
        else:
            response = client.post(
                "/agents",
                json={
                    "schema_version": 1,
                    "config": config.model_dump(mode="json"),
                    "expected_version": 0,
                },
                headers={"Idempotency-Key": "config"},
            )
            assert response.status_code == 201, response.text
            config_id = response.json()["config_id"]
        assert service.research.config(UUID(repo), UUID(config_id)) == config
        response = client.post(
            "/tasks",
            json={
                "schema_version": 1,
                "config_id": config_id,
                "config_version": 1,
                "expected_version": 0,
                "goal": {"schema_version": 1, "text": "计算三框架统计并整理报告"},
            },
            headers={"Idempotency-Key": "task"},
        )
        assert response.status_code == 201, response.text
        task = response.json()["task_id"]
        with pytest.raises(AgentGitError) as rejected:
            service.research.start(UUID(repo), UUID(task))
        assert rejected.value.code == ErrorCode.FORBIDDEN
        payload = {
            "schema_version": 1,
            "config_version": 1,
            "expected_branch_version": 0,
            "expected_head": None,
        }
        submitted = client.post(
            f"/tasks/{task}/runs", json=payload, headers={"Idempotency-Key": "run"}
        )
        assert submitted.status_code == 202, submitted.text
        assert (
            client.post(
                f"/tasks/{task}/runs", json=payload, headers={"Idempotency-Key": "run"}
            ).json()
            == submitted.json()
        )
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
                ).scalar_one()
                == 0
            )
        assert Worker(service.jobs).run_once()["status"] == "completed"
        assert Worker(service.jobs).run_once() is None
        run = submitted.json()["run_id"]
        status = client.get(f"/runs/{run}").json()
        assert status["budget"]["usage"]["tool_calls"] == 13
        artifacts = client.get(f"/runs/{run}/artifacts").json()["items"]
        assert len(artifacts) == 7
        for item in artifacts:
            downloaded = client.get(f"/artifacts/{item['id']}/content")
            assert downloaded.status_code == 200
            assert object_digest(ObjectType.BLOB, downloaded.content) == item["object_id"]
        statistics = next(
            item for item in artifacts if item["relative_path"] == "results/statistics.json"
        )
        assert client.get(f"/artifacts/{statistics['id']}/content").json()["claims"] == 9
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
                ).scalar_one()
                == 2
            )


def test_configuration_cannot_select_an_unreviewed_image(history, tmp_path):
    research = ResearchService(history, tmp_path / "config-boundary")
    repository = history.create_repository("拒绝替换固定镜像")
    policy = bundled_policy().model_copy(update={"image_id": "sha256:" + "f" * 64})
    config = AgentConfig(tools=("code_exec",), execution_policy=policy)
    with pytest.raises(AgentGitError) as rejected:
        research.create_config(repository, config)
    assert rejected.value.code == ErrorCode.FORBIDDEN
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["agent_configs"])
            ).scalar_one()
            == 0
        )
