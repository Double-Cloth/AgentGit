"""S6 读取入口：同时间戳游标、项目隔离及历史同名产物。"""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN, prepare
from test_management_api import api as api
from test_versions import versioned as versioned

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration


def test_run_pagination_with_tied_timestamps_is_read_only_and_project_scoped(api):
    client, manager, _ = api
    history = manager.history
    repo, other = history.create_repository("列表项目"), history.create_repository("其他项目")
    identities, foreign = [uuid4() for _ in range(17)], uuid4()
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["runs"].insert(),
            [
                {
                    "id": identity,
                    "repository_id": repo,
                    "branch_name": "main",
                    "mode": "live",
                    "status": "completed",
                }
                for identity in identities
            ]
            + [
                {
                    "id": foreign,
                    "repository_id": other,
                    "branch_name": "main",
                    "mode": "live",
                    "status": "completed",
                }
            ],
        )
        before = list(
            connection.execute(sa.select(TABLES["runs"]).order_by(TABLES["runs"].c.id)).mappings()
        )
    seen, cursor = [], None
    while True:
        response = client.get(
            f"/repositories/{repo}/runs",
            params={"limit": 3, **({"after_id": cursor} if cursor else {})},
            headers={"X-Repository-ID": str(repo)},
        )
        assert response.status_code == 200, response.text
        value = response.json()
        seen.extend(item["id"] for item in value["items"])
        assert all(item["goal"] is None and item["job_id"] is None for item in value["items"])
        cursor = value["next_after_id"]
        if cursor is None:
            break
    assert seen == [str(identity) for identity in sorted(identities, reverse=True)]
    for path, params in (
        (f"/repositories/{repo}/runs", {"after_id": str(foreign)}),
        (f"/repositories/{other}/runs", {}),
    ):
        assert (
            client.get(path, params=params, headers={"X-Repository-ID": str(repo)}).status_code
            == 404
        )
    with history.engine.connect() as connection:
        assert (
            list(
                connection.execute(
                    sa.select(TABLES["runs"]).order_by(TABLES["runs"].c.id)
                ).mappings()
            )
            == before
        )


def test_queued_job_list_exposes_goal_and_job_without_starting_worker(api):
    client, manager, _ = api
    repo, _, _, submitted = prepare(api)
    response = client.get(f"/repositories/{repo}/runs", headers={"X-Repository-ID": repo})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == submitted["run_id"] and item["job_id"] == submitted["job_id"]
    assert item["status"] == item["job_status"] == "queued"
    assert item["goal"]["text"] == "比较三个 fixture 框架"
    assert not {"worker", "payload_ref", "lease_until"}.intersection(item)
    assert not (manager.research.data_root / "workspaces").exists()


def test_capabilities_require_identity_and_do_not_claim_worker_or_sandbox_ready(api, monkeypatch):
    client, manager, _ = api

    def forbidden(*args, **kwargs):
        pytest.fail("能力描述不能查询数据库或探测容器引擎")

    with monkeypatch.context() as no_probe:
        no_probe.setattr(manager.history.engine, "connect", forbidden)
        no_probe.setattr("agentgit.adapters.execution.engine.DockerEngine.__init__", forbidden)
        response = client.get("/capabilities")
    assert response.status_code == 200, response.text
    descriptor = response.json()
    assert descriptor["models"][0]["external"] is False
    real_model = descriptor["models"][1]
    assert real_model["access"] == "unknown" and real_model["currency"] == "USD"
    assert real_model["minimum_reservation"]["cost_micro_usd"] == 61_440
    assert "max_cost_micro_usd" not in descriptor["default_config"]["limits"]
    assert "api_key" not in str(descriptor)
    assert (
        descriptor["worker"]["status"] == descriptor["execution_environment"]["status"] == "unknown"
    )
    assert descriptor["default_config"]["execution_policy"] is None
    assert set(descriptor["execution_config"]["tools"]) == {
        "corpus_search",
        "source_read",
        "artifact_write",
        "code_exec",
        "terminal_exec",
    }
    assert client.get("/capabilities", headers={"Authorization": "Bearer wrong"}).status_code == 401
    repo = manager.history.create_repository("使用能力描述创建五工具配置")
    created = client.post(
        "/agents",
        json={"schema_version": 1, "expected_version": 0, "config": descriptor["execution_config"]},
        headers={"X-Repository-ID": str(repo), "Idempotency-Key": "capability-config"},
    )
    assert created.status_code == 201, created.text
    info = client.get(f"/repositories/{repo}", headers={"X-Repository-ID": str(repo)})
    assert info.json()["config_version"] == 1
    second = client.post(
        "/agents",
        json={"schema_version": 1, "expected_version": 1, "config": descriptor["default_config"]},
        headers={"X-Repository-ID": str(repo), "Idempotency-Key": "capability-config-second"},
    )
    assert second.status_code == 201, second.text
    assert (
        client.get(f"/repositories/{repo}", headers={"X-Repository-ID": str(repo)}).json()[
            "config_version"
        ]
        == 2
    )


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"after_id": "not-a-uuid"}])
def test_run_list_rejects_invalid_pagination_before_query(api, query):
    client, manager, _ = api
    repo = manager.history.create_repository("分页边界")
    assert (
        client.get(
            f"/repositories/{repo}/runs", params=query, headers={"X-Repository-ID": str(repo)}
        ).status_code
        == 422
    )


def test_historical_artifact_list_downloads_selected_version_and_rejects_foreign_cursor(
    history, tmp_path, versioned
):
    _, repo, run, commits, _ = versioned
    manager = ManagementService(ResearchService(history, tmp_path / "history-api"))
    original = history.branch(repo)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        first = client.get(f"/commits/{commits[0]}/artifacts").json()
        latest = client.get(f"/commits/{commits[-1]}/artifacts").json()
        assert first["commit_id"] == commits[0] and first["next_after_id"] is None
        assert first["items"][0]["relative_path"] == latest["items"][0]["relative_path"] == "binary"
        assert first["items"][0]["object_id"] != latest["items"][0]["object_id"]
        assert client.get(f"/artifacts/{first['items'][0]['id']}/content").content == bytes(
            [255, 0]
        )
        assert client.get(f"/artifacts/{latest['items'][0]['id']}/content").content == bytes(
            [255, 2]
        )
        assert (
            client.get(
                f"/commits/{commits[0]}/artifacts", params={"after_id": latest["items"][0]["id"]}
            ).status_code
            == 404
        )
        other = history.create_repository("其他项目")
        assert (
            client.get(
                f"/commits/{commits[0]}/artifacts", headers={"X-Repository-ID": str(other)}
            ).status_code
            == 404
        )
        assert client.get(f"/runs/{run}/executions").json() == {"items": [], "next_after_id": None}
        assert client.get(f"/runs/{uuid4()}/executions").status_code == 404
        assert (
            client.get(
                f"/repositories/{repo}/runs", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
    assert history.branch(repo) == original
