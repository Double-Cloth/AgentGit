"""修复模板 API/CLI 的实际发布、项目边界、单连接池与请求重试。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN
from test_repair_worker import create_case, drive
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.repair import NewRepairSearch
from agentgit.services.history import HistoryService
from agentgit.services.management import ManagementService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def public_request(history, repo, search):
    view = RepairJobService(history).view(repo, search)
    manifest = view["manifest"]
    binding = manifest["space"]["binding"]
    # 原来源准备服务已经演练内部发布；这里只保留未派发的来源，另走公共模板入口。
    RepairJobService(history).cancel(repo, search, actor="fixture")
    return {
        "schema_version": 1,
        "failed_run_id": binding["failed_run_id"],
        "base_commit": binding["base_commit"],
        "validator_ref": binding["validator_ref"],
        "targets": [
            {
                "schema_version": 1,
                "event_id": manifest["space"]["patches"][0]["intervention"]["target_event_id"],
            }
        ],
    }


def row_count(history, name):
    with history.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(TABLES[name])).scalar_one()


@pytest.mark.parametrize("fixture_scope", [None, "stochastic_fixture"])
def test_public_http_templates_to_independent_worker_and_readonly_cli(
    history, tmp_path, monkeypatch, fixture_scope
):
    repo, old, research, _ = create_case(history, tmp_path)
    body = public_request(history, repo, old)
    if fixture_scope is not None:
        body["fixture_scope"] = fixture_scope
    manager = ManagementService(research)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        jobs_before = row_count(history, "jobs")

        def forbidden(*args, **kwargs):
            pytest.fail("提交和读取搜索不能执行模型或工具")

        with monkeypatch.context() as submission:
            submission.setattr(MockResearchModel, "invoke", forbidden)
            submission.setattr(ToolRegistry, "invoke", forbidden)
            response = client.post(
                "/repair-searches", json=body, headers={"Idempotency-Key": "http-search"}
            )
        assert response.status_code == 202, response.text
        created = response.json()
        search = UUID(created["search_id"])
        assert created["proposal_count"] == 2 and row_count(history, "jobs") == jobs_before
        assert (
            client.post(
                "/repair-searches", json=body, headers={"Idempotency-Key": "http-search"}
            ).json()
            == created
        )
        changed = {**body, "max_depth": 1}
        assert (
            client.post(
                "/repair-searches", json=changed, headers={"Idempotency-Key": "http-search"}
            ).status_code
            == 409
        )
        ready = client.get(f"/repair-searches/{search}").json()
        assert ready["manifest"].get("fixture_scope") == fixture_scope
        assert not ready["trials"]
        if fixture_scope is not None:
            omitted_scope = {k: v for k, v in body.items() if k != "fixture_scope"}
            assert (
                client.post(
                    "/repair-searches",
                    json=omitted_scope,
                    headers={"Idempotency-Key": "http-search"},
                ).status_code
                == 409
            )
        assert ready["manifest"]["space"]["binding"]["policy_version"].startswith(
            "bounded-templates/mock-bytes-v2@"
        )
        assert ready["manifest"]["space"]["template_skips"]
        result = drive(research, repo, search)
        assert result["status"] == "needs_confirmation"

        def readonly(*args, **kwargs):
            pytest.fail("读取已完成搜索不能写 CAS")

        with monkeypatch.context() as read:
            read.setattr(history.objects, "put", readonly)
            view = client.get(f"/repair-searches/{search}").json()
            assert view["decision"]["selected_proposal_id"]
            assert (
                client.get(
                    f"/repair-searches/{search}", headers={"X-Repository-ID": str(uuid4())}
                ).status_code
                == 404
            )
            listed = client.get(f"/repositories/{repo}/repair-searches", params={"limit": 1}).json()
            assert len(listed["items"]) == 1 and listed["next_after_id"]
            last = client.get(
                f"/repositories/{repo}/repair-searches",
                params={"after_id": listed["next_after_id"]},
            ).json()
            assert len(last["items"]) == 1 and last["next_after_id"] is None
            assert (
                client.get(
                    f"/repositories/{repo}/repair-searches",
                    headers={"X-Repository-ID": str(uuid4())},
                ).status_code
                == 404
            )

        @contextmanager
        def current(testing):
            yield manager

        monkeypatch.setattr("agentgit.cli.main.management", current)
        status = CliRunner().invoke(app, ["repair-status", str(repo), str(search)])
        assert status.exit_code == 0, status.output
        assert json.loads(status.output)["decision"] == view["decision"]
        listed = CliRunner().invoke(app, ["repair-list", str(repo)])
        assert listed.exit_code == 0 and len(json.loads(listed.output)["items"]) == 2


def test_public_template_submission_and_cancel_work_in_single_connection_pool(history, tmp_path):
    repo, old, research, _ = create_case(history, tmp_path)
    body = NewRepairSearch.model_validate_json(encode(public_request(history, repo, old)))
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    engine = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        from agentgit.services.research import ResearchService

        manager = ManagementService(
            ResearchService(HistoryService(engine, history.objects), research.data_root)
        )
        before = row_count(history, "interventions")
        with ThreadPoolExecutor(max_workers=6) as pool:
            created = list(
                pool.map(
                    lambda _: manager.create_repair_search(repo, body, "single-pool"), range(6)
                )
            )
        assert all(item == created[0] for item in created)
        assert row_count(history, "interventions") == before + 2
        search = UUID(created[0]["search_id"])
        first = manager.cancel_repair_search(repo, search, "cancel-key")
        assert first["status"] == "cancelled"
        assert manager.cancel_repair_search(repo, search, "cancel-key") == first
        # 已缓存请求不再生成模板，也不受后续代码/模板版本变化影响。
        assert manager.create_repair_search(repo, body, "single-pool") == created[0]
    finally:
        engine.dispose()


def test_http_rejects_arbitrary_patches_foreign_events_and_missing_auth_without_job(
    history, tmp_path
):
    repo, old, research, _ = create_case(history, tmp_path)
    body = public_request(history, repo, old)
    manager = ManagementService(research)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
    ) as client:
        headers = {
            "Authorization": "Bearer " + TOKEN,
            "X-Repository-ID": str(repo),
            "Idempotency-Key": "rejected",
        }
        jobs_before, searches_before = (
            row_count(history, "jobs"),
            row_count(history, "repair_searches"),
        )
        assert (
            client.post(
                "/repair-searches", json=body, headers={"X-Repository-ID": str(repo)}
            ).status_code
            == 401
        )
        for field in ("validator", "tools", "patches", "permissions", "plan"):
            assert (
                client.post(
                    "/repair-searches", json={**body, field: {}}, headers=headers
                ).status_code
                == 422
            )
        foreign = deepcopy(body)
        foreign["targets"][0]["event_id"] = 9_000_000_000
        assert client.post("/repair-searches", json=foreign, headers=headers).status_code == 404
        assert (
            row_count(history, "jobs") == jobs_before
            and row_count(history, "repair_searches") == searches_before
        )


def test_cli_template_submit_worker_once_status_and_cancel(history, tmp_path, monkeypatch):
    repo, old, research, _ = create_case(history, tmp_path)
    body = public_request(history, repo, old)
    manager = ManagementService(research)

    @contextmanager
    def current(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", current)
    file = tmp_path / "search.json"
    file.write_bytes(encode(body))
    runner = CliRunner()
    created = runner.invoke(
        app, ["repair-search", str(repo), "--file", str(file), "--key", "cli-search"]
    )
    assert created.exit_code == 0, created.output
    search = json.loads(created.output)["search_id"]
    tick = runner.invoke(app, ["repair-worker", "--once", "--repository-id", str(repo)])
    assert tick.exit_code == 0 and json.loads(tick.output)["usage"]["started"]["screen"] == 1
    cancelled = runner.invoke(app, ["repair-cancel", str(repo), search, "--key", "cli-cancel"])
    assert cancelled.exit_code == 0 and json.loads(cancelled.output)["cancel_requested"]
