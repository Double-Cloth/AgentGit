"""S5 版本入口共用幂等事务，不执行任务或历史命令。"""

import json

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_versions import versioned as versioned
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.versions import NewBranch
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration


def test_api_cli_fork_history_checkout_and_diff_share_state(versioned, tmp_path, monkeypatch):
    versions, repository, run, commits, _ = versioned
    service = ManagementService(ResearchService(versions.history, tmp_path / "version-api"))
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    token = "fixture-versions-api-token-0000000000000000000"
    settings = Settings(api_token=SecretStr(token))
    with TestClient(
        create_app(settings, management=service),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(repository)},
    ) as client:
        payload = NewBranch(
            source_run_id=str(run), from_commit=commits[1], name="http-fork", expected_version=0
        ).model_dump(mode="json")
        created = client.post("/branches", json=payload, headers={"Idempotency-Key": "fork"})
        assert created.status_code == 201, created.text
        assert (
            client.post("/branches", json=payload, headers={"Idempotency-Key": "fork"}).json()
            == created.json()
        )
        assert (
            client.post(
                "/branches",
                json={**payload, "name": "changed"},
                headers={"Idempotency-Key": "fork"},
            ).status_code
            == 409
        )
        result = CliRunner().invoke(
            app,
            [
                "branch",
                str(repository),
                "--name",
                "cli-fork",
                "--source-run",
                str(run),
                "--from-commit",
                commits[1],
                "--key",
                "cli-fork",
            ],
        )
        assert result.exit_code == 0, (result.stdout, result.exception)
        assert json.loads(result.stdout)["head_commit_id"] == commits[1]
        branches = client.get(f"/repositories/{repository}/branches", params={"limit": 2}).json()
        assert branches["has_more"] and len(branches["items"]) == 2
        remaining = client.get(
            f"/repositories/{repository}/branches",
            params={"after_name": branches["next_after_name"], "limit": 2},
        ).json()
        assert not remaining["has_more"] and len(remaining["items"]) == 1
        for command, arguments, endpoint in (
            ("log", [], f"/repositories/{repository}/commits"),
            ("checkout", [commits[1]], f"/commits/{commits[1]}"),
        ):
            result = CliRunner().invoke(app, [command, str(repository), *arguments])
            assert result.exit_code == 0, result.stdout
            assert json.loads(result.stdout) == client.get(endpoint).json()
        result = CliRunner().invoke(app, ["diff", str(repository), commits[0], commits[2]])
        assert result.exit_code == 0, result.stdout
        assert (
            json.loads(result.stdout)
            == client.get(
                "/diff", params={"left_commit": commits[0], "right_commit": commits[2]}
            ).json()
        )
    assert not (service.research.data_root / "workspaces").exists()
    assert versions.history.branch(repository)["head_commit_id"] == commits[2]


def test_fork_and_idempotent_response_roll_back_together(versioned, tmp_path, monkeypatch):
    versions, repository, run, commits, _ = versioned
    service = ManagementService(ResearchService(versions.history, tmp_path / "rollback"))
    original = versions.history.objects.put

    def failed(kind, payload, *args, **kwargs):
        if kind == ObjectType.BLOB and b'"name":"rollback"' in payload:
            raise RuntimeError("响应保存失败")
        return original(kind, payload, *args, **kwargs)

    monkeypatch.setattr(versions.history.objects, "put", failed)
    with pytest.raises(RuntimeError, match="响应保存失败"):
        service.create_branch(
            repository,
            NewBranch(
                source_run_id=str(run), from_commit=commits[0], name="rollback", expected_version=0
            ),
            "rollback",
        )
    with versions.history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branch_forks"])
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                sa.select(TABLES["branches"].c.name).where(TABLES["branches"].c.name == "rollback")
            ).first()
            is None
        )
        assert (
            connection.execute(
                sa.select(TABLES["idempotency_keys"].c.key).where(
                    TABLES["idempotency_keys"].c.key == "rollback"
                )
            ).first()
            is None
        )
