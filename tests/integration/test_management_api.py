"""S4：真实数据库、业务 HTTP 边界与独立于请求的 Worker。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_approvals import action as action
from test_approvals import effect_server as effect_server
from test_approvals import pending

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, object_digest
from agentgit.core.research import AgentConfig
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.management import ManagementService
from agentgit.services.recovery import prepare_recovery
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
TOKEN = "fixture-local-api-token-00000000000000000000"


@pytest.fixture
def api(history, tmp_path):
    root = tmp_path / "api-data"
    service = ManagementService(
        ResearchService(HistoryService(history.engine, ObjectStore(root / "objects")), root)
    )
    settings = Settings(api_token=SecretStr(TOKEN))
    with TestClient(
        create_app(settings, management=service),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        yield client, service, settings


def post(client, path, body, key, repo=None):
    headers = {"Idempotency-Key": key}
    if repo is not None:
        headers["X-Repository-ID"] = repo
    return client.post(path, json={"schema_version": 1, **body}, headers=headers)


def prepare(api, budget=None):
    client, _, _ = api
    response = post(client, "/repositories", {"name": "HTTP 研究", "expected_version": 0}, "repo-1")
    assert response.status_code == 201, response.text
    repo = response.json()["repository_id"]
    response = post(
        client,
        "/agents",
        {"config": AgentConfig().model_dump(mode="json"), "expected_version": 0},
        "config-1",
        repo,
    )
    assert response.status_code == 201, response.text
    config = response.json()["config_id"]
    response = post(
        client,
        "/tasks",
        {
            "config_id": config,
            "config_version": 1,
            "expected_version": 0,
            "goal": {"schema_version": 1, "text": "比较三个 fixture 框架"},
        },
        "task-1",
        repo,
    )
    assert response.status_code == 201, response.text
    task = response.json()["task_id"]
    response = post(
        client,
        f"/tasks/{task}/runs",
        {
            "config_version": 1,
            "expected_branch_version": 0,
            "expected_head": None,
            "budget": budget,
        },
        "run-1",
        repo,
    )
    assert response.status_code == 202, response.text
    return repo, config, task, response.json()


def test_api_restart_worker_completion_events_and_artifact_hash(api):
    client, service, settings = api
    repo, _, _, submitted = prepare(api)
    assert not (service.research.data_root / "workspaces").exists()
    with TestClient(
        create_app(settings, management=service),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": repo},
    ) as restarted:
        assert restarted.get(f"/jobs/{submitted['job_id']}").json()["status"] == "queued"
        assert Worker(service.jobs).run_once()["status"] == "completed"
        result = restarted.get(f"/runs/{submitted['run_id']}")
        assert result.status_code == 200, result.text
        assert result.json()["budget"]["usage"]["model_calls"] == 11
        assert result.json()["budget"]["usage"]["tool_calls"] == 9
        assert result.json()["budget"]["usage"]["reserved_tokens"] == 0
        after, seqs = 0, []
        while True:
            page = restarted.get(
                f"/runs/{submitted['run_id']}/events", params={"after_seq": after, "limit": 7}
            ).json()
            assert (
                restarted.get(
                    f"/runs/{submitted['run_id']}/events", params={"after_seq": after, "limit": 7}
                ).json()
                == page
            )
            if not page["items"]:
                break
            seqs.extend(item["seq"] for item in page["items"])
            after = page["next_after_seq"]
        assert seqs == list(range(1, result.json()["last_event_seq"] + 1))
        artifacts = restarted.get(f"/runs/{submitted['run_id']}/artifacts").json()["items"]
        assert {item["relative_path"] for item in artifacts} == {"report.md", "sources.json"}
        for artifact in artifacts:
            response = restarted.get(f"/artifacts/{artifact['id']}/content")
            assert response.status_code == 200
            assert object_digest(ObjectType.BLOB, response.content) == artifact["object_id"]
            assert response.headers["ETag"] == f'"{artifact["object_id"]}"'
            assert response.headers["content-disposition"].startswith("attachment;")


def test_all_creation_and_control_actions_are_idempotent(api):
    client, service, _ = api
    repo, _, task, submitted = prepare(api)
    original = post(client, "/repositories", {"name": "HTTP 研究", "expected_version": 0}, "repo-1")
    assert original.json()["repository_id"] == repo
    assert (
        post(
            client, "/repositories", {"name": "改参数", "expected_version": 0}, "repo-1"
        ).status_code
        == 409
    )
    duplicate = post(
        client,
        f"/tasks/{task}/runs",
        {"config_version": 1, "expected_branch_version": 0, "expected_head": None},
        "run-1",
        repo,
    )
    assert duplicate.json() == submitted
    run = submitted["run_id"]
    paused = post(client, f"/runs/{run}/pause", {"expected_version": 0}, "pause-1", repo)
    assert paused.status_code == 202, paused.text
    assert paused.json()["control_status"] == "requested"
    assert Worker(service.jobs).run_once()["status"] == "paused"
    assert (
        post(client, f"/runs/{run}/pause", {"expected_version": 0}, "pause-1", repo).json()
        == paused.json()
    )
    assert (
        post(
            client, f"/runs/{run}/resume", {"expected_version": 0}, "stale-resume", repo
        ).status_code
        == 409
    )
    current = client.get(f"/runs/{run}", headers={"X-Repository-ID": repo}).json()
    resumed = post(
        client,
        f"/runs/{run}/resume",
        {"expected_version": current["state_version"]},
        "resume-1",
        repo,
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["creates_new_run"] is False
    assert Worker(service.jobs).run_once()["status"] == "completed"
    with service.history.engine.connect() as connection:
        for name in ("repositories", "agent_configs", "tasks", "runs", "jobs"):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                == 1
            )


@pytest.mark.parametrize(
    "bad", ["missing_token", "wrong_token", "origin", "host", "duplicate_auth"]
)
def test_security_boundary_rejects_before_database_access(api, bad):
    _, service, settings = api
    headers = [("Authorization", "Bearer " + TOKEN)]
    if bad == "missing_token":
        headers = []
    elif bad == "wrong_token":
        headers = [("Authorization", "Bearer private-invalid-token")]
    elif bad == "origin":
        headers.append(("Origin", "https://evil.example"))
    elif bad == "host":
        headers.append(("Host", "evil.example"))
    else:
        headers.append(("Authorization", "Bearer duplicate"))
    with TestClient(
        create_app(settings, management=service), base_url="http://127.0.0.1"
    ) as client:
        response = client.post("/repositories", headers=headers, json={"secret": "private-body"})
    assert response.status_code in (401, 403, 422)
    UUID(response.headers["x-correlation-id"])
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert "private" not in response.text
    with service.history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["repositories"])
            ).scalar_one()
            == 0
        )


def test_origin_preflight_and_wrong_project_ids(api):
    client, _, _ = api
    response = client.options(
        "/repositories",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type, Idempotency-Key",
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers
    repo, config, _, submitted = prepare(api)
    other = post(
        client, "/repositories", {"name": "其他项目", "expected_version": 0}, "repo-2"
    ).json()["repository_id"]
    for path in (
        f"/runs/{submitted['run_id']}",
        f"/jobs/{submitted['job_id']}",
        f"/runs/{submitted['run_id']}/events",
        f"/runs/{submitted['run_id']}/artifacts",
    ):
        response = client.get(path, headers={"X-Repository-ID": other})
        assert response.status_code == 404, response.text
    assert (
        post(
            client,
            "/tasks",
            {
                "config_id": config,
                "config_version": 1,
                "expected_version": 0,
                "goal": {"schema_version": 1, "text": "跨项目"},
            },
            "foreign-task",
            other,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/runs/{submitted['run_id']}/events",
            headers={"X-Repository-ID": repo},
            params={"limit": 1001},
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":1,"expected_version":0.1}',
        '{"schema_version":2}',
        '{"schema_version":1,"expected_version":false,"name":"x"}',
    ],
)
def test_raw_json_contract_rejects_ambiguous_input(api, raw):
    client, _, _ = api
    response = client.post(
        "/repositories",
        content=raw.encode(),
        headers={"Content-Type": "application/json", "Idempotency-Key": "raw"},
    )
    assert response.status_code in (409, 422), response.text


def test_oversized_body_is_rejected_even_without_content_length(api):
    client, _, _ = api
    response = client.post(
        "/repositories",
        content=(b"x" * 300000 for _ in range(4)),
        headers={"Content-Type": "application/json", "Idempotency-Key": "large"},
    )
    assert response.status_code == 413, response.text


def test_mutation_and_idempotency_response_roll_back_together(api, monkeypatch):
    client, service, _ = api
    original = service.history._ref

    def fail(*args, **kwargs):
        raise RuntimeError("private-database-details")

    with monkeypatch.context() as patch:
        patch.setattr(service.history, "_ref", fail)
        response = post(
            client, "/repositories", {"name": "原子创建", "expected_version": 0}, "atomic"
        )
    assert response.status_code == 500
    assert "private" not in response.text
    with service.history.engine.connect() as connection:
        for name in ("repositories", "branches", "idempotency_keys"):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                == 0
            )
    assert service.history._ref == original
    assert (
        post(
            client, "/repositories", {"name": "原子创建", "expected_version": 0}, "atomic"
        ).status_code
        == 201
    )


def test_concurrent_repository_submission_has_one_result(api):
    client, _, _ = api
    with ThreadPoolExecutor(2) as pool:
        responses = list(
            pool.map(
                lambda _: post(
                    client, "/repositories", {"name": "并发", "expected_version": 0}, "concurrent"
                ),
                range(2),
            )
        )
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json() == responses[1].json()


def test_approval_inspection_and_decision_share_idempotent_transaction(action, tmp_path):
    history, repo, run, _, gateway, options, _, counts, _, _ = action
    row, version = pending(action)
    manager = ManagementService(
        ResearchService(history, tmp_path / "approval-api"), actor="fixture-operator"
    )
    settings = Settings(api_token=SecretStr(TOKEN), operator_id="fixture-operator")
    with TestClient(
        create_app(settings, management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        page = client.get("/approvals", params={"run_id": str(run), "limit": 1})
        assert page.status_code == 200
        item = page.json()["items"][0]
        assert item["request"]["value"] == "approved-value"
        assert item["action"]["actor"] == "fixture-operator"
        decision = post(
            client,
            f"/approvals/{row['id']}/decision",
            {"decision": "approve", "expected_version": version},
            "approve-1",
            str(repo),
        )
        assert decision.status_code == 202, decision.text
        assert gateway.call(**options).error is None
        assert (
            post(
                client,
                f"/approvals/{row['id']}/decision",
                {"decision": "approve", "expected_version": version},
                "approve-1",
                str(repo),
            ).json()
            == decision.json()
        )
        assert (
            post(
                client,
                f"/approvals/{row['id']}/decision",
                {"decision": "reject", "expected_version": version},
                "approve-1",
                str(repo),
            ).status_code
            == 409
        )
        assert counts()["requests"] == 1
        assert (
            client.get(
                "/approvals",
                params={"run_id": str(run), "after_seq": page.json()["next_after_seq"]},
            ).json()["items"]
            == []
        )


def test_metrics_and_logs_do_not_record_tokens_or_request_body(api, caplog):
    client, _, _ = api
    caplog.set_level("INFO", logger="agentgit.http")
    repo = post(
        client, "/repositories", {"name": "private-request-body", "expected_version": 0}, "metrics"
    ).json()["repository_id"]
    response = client.get("/metrics", headers={"X-Repository-ID": repo})
    assert response.status_code == 200
    assert response.json()["http"]["requests_total"] >= 1
    assert response.json()["jobs"] == {}
    text = " ".join(record.message for record in caplog.records)
    assert TOKEN not in text and "private-request-body" not in text
    assert "correlation_id" in text


def test_maximum_idempotency_key_is_bounded_without_truncation(api):
    client, _, _ = api
    body = {"name": "长幂等键", "expected_version": 0}
    key = "k" * 200
    first = post(client, "/repositories", body, key)
    assert first.status_code == 201, first.text
    assert post(client, "/repositories", body, key).json() == first.json()
    assert post(client, "/repositories", body, key + "x").status_code == 422


def test_explicit_run_budget_can_only_tighten_task_limits(api):
    client, service, _ = api
    repo, _, task, submitted = prepare(api, {"schema_version": 1, "max_steps": 1})
    before = client.get(f"/runs/{submitted['run_id']}", headers={"X-Repository-ID": repo}).json()
    assert before["budget"]["limits"]["max_steps"] == 1
    assert Worker(service.jobs).run_once()["status"] == "budget_exhausted"
    branch = service.history.branch(UUID(repo))
    enlarged = post(
        client,
        f"/tasks/{task}/runs",
        {
            "config_version": 1,
            "expected_branch_version": branch["version"],
            "expected_head": branch["head_commit_id"],
            "budget": {"schema_version": 1, "max_steps": 33},
        },
        "enlarge",
        repo,
    )
    assert enlarged.status_code == 403
    assert client.get("/metrics", headers={"X-Repository-ID": repo}).json()["jobs"] == {
        "budget_exhausted": 1
    }


@pytest.mark.parametrize("action", ["agent", "task", "control"])
def test_every_shared_mutation_rolls_back_before_response_commit(api, monkeypatch, action):
    client, service, _ = api
    repo, config, _, submitted = prepare(api)
    original = service.history._ref
    names = ("agent_configs", "tasks", "events", "idempotency_keys")
    with service.history.engine.connect() as connection:
        before = {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in names
        }

    def fail_response(*args):
        if args[3] == "request":
            raise RuntimeError("注入响应落库失败")
        return original(*args)

    options = {
        "agent": ("/agents", {"expected_version": 1}),
        "task": (
            "/tasks",
            {
                "config_id": config,
                "config_version": 1,
                "expected_version": 0,
                "goal": {"schema_version": 1, "text": "另一个任务"},
            },
        ),
        "control": (f"/runs/{submitted['run_id']}/pause", {"expected_version": 0}),
    }
    path, body = options[action]
    with monkeypatch.context() as patch:
        patch.setattr(service.history, "_ref", fail_response)
        assert post(client, path, body, "rollback", repo).status_code == 500
    with service.history.engine.connect() as connection:
        after = {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in names
        }
    assert before == after
    assert service.jobs.run(UUID(repo), UUID(submitted["run_id"]))["control_status"] is None
    assert post(client, path, body, "rollback", repo).status_code in (201, 202)


def test_new_recovery_run_does_not_display_previous_task_budget(api):
    client, service, _ = api
    repo, config, _, _ = prepare(api)
    assert Worker(service.jobs).run_once()["status"] == "completed"
    branch = service.history.branch(UUID(repo))
    task = post(
        client,
        "/tasks",
        {
            "config_id": config,
            "config_version": 1,
            "expected_version": 0,
            "goal": {"schema_version": 1, "text": "新的独立研究"},
        },
        "task-second",
        repo,
    ).json()["task_id"]
    submitted = post(
        client,
        f"/tasks/{task}/runs",
        {
            "config_version": 1,
            "expected_branch_version": branch["version"],
            "expected_head": branch["head_commit_id"],
        },
        "run-second",
        repo,
    ).json()
    first = service.jobs.claim("crashed")
    with service.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted["job_id"]))
            .values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
        )
    second = service.jobs.claim("recovery")
    recovered = prepare_recovery(service.jobs, second)
    result = client.get(f"/runs/{recovered.run_id}", headers={"X-Repository-ID": repo})
    assert result.status_code == 200, result.text
    assert result.json()["budget"]["usage"]["model_calls"] == 0
    assert recovered.run_id != first.run_id
    project = client.get(f"/repositories/{repo}", headers={"X-Repository-ID": repo}).json()
    assert project["branch"]["version"] == branch["version"]
