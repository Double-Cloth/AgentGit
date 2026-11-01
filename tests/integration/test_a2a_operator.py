"""本人HTTP／CLI入口、真实协议服务和通用查询／控制访问边界。"""

import copy
import json
import os
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_a2a_fixture import CASES
from test_a2a_fixture import server as server
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.a2a import A2ABinding, A2AServer
from agentgit.core.a2a_runs import A2AProgram, NewA2ARun
from agentgit.core.canonical import encode
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "a2a-operator"
TOKEN = "fixture-operator-api-token-000000000000000000"


@contextmanager
def api_for(manager):
    with TestClient(
        create_app(
            Settings(operator_id=manager.actor, api_token=SecretStr(TOKEN)), management=manager
        ),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        yield client


@pytest.fixture
def operator(history, server, tmp_path):
    manager = ManagementService(ResearchService(history, tmp_path), actor=ACTOR)
    repo = history.create_repository("A2A 操作者入口")
    config = A2AServer(
        server_identity="local-research",
        principal_id="operator",
        endpoint=f"http://127.0.0.1:{server[3]['port']}",
    )
    with api_for(manager) as client:
        bound = client.post("/a2a/bind", json=config.model_dump(mode="json"))
        assert bound.status_code == 200, bound.text
        body = copy.deepcopy(CASES["request"])
        body["message"]["messageId"] = str(uuid4())
        program = A2AProgram(
            binding=A2ABinding.model_validate_json(bound.content), messages=(encode(body).decode(),)
        )
        yield client, manager, repo, program, server


def post(client, repo, path, body, key):
    return client.post(
        path, json=body, headers={"X-Repository-ID": str(repo), "Idempotency-Key": key}
    )


def get(client, repo, path, **params):
    return client.get(path, params=params, headers={"X-Repository-ID": str(repo)})


def submit(operator, key="operator-live"):
    client, _, repo, program, _ = operator
    request = NewA2ARun(program=program, expected_head=None, expected_version=0).model_dump(
        mode="json"
    )
    response = post(client, repo, f"/repositories/{repo}/a2a-runs", request, key)
    assert response.status_code == 202, response.text
    return response.json(), request


def work(manager):
    deadline = time.perf_counter() + 10
    while True:
        result = Worker(manager.jobs).run_once()
        if result is not None:
            return result
        assert time.perf_counter() < deadline, "A2A Job未按原计划可领取"
        time.sleep(0.01)


def approve(client, manager, repo, receipt, key="approve"):
    result = get(client, repo, "/approvals", run_id=receipt["run_id"])
    assert result.status_code == 200, result.text
    approval = result.json()["items"][-1]
    response = post(
        client,
        repo,
        f"/approvals/{approval['id']}/decision",
        {"schema_version": 1, "expected_version": approval["version"], "decision": "approve"},
        key,
    )
    assert response.status_code == 202, response.text
    return approval


def finish_live(operator):
    client, manager, repo, _, _ = operator
    receipt, request = submit(operator)
    assert work(manager)["status"] == "waiting_approval"
    approve(client, manager, repo, receipt)
    assert work(manager)["status"] == "completed"
    return receipt, get(client, repo, f"/runs/{receipt['run_id']}").json(), request


def test_http_live_original_key_approval_then_full_recorded_projection(operator):
    client, manager, repo, _, server = operator
    live, source, request = finish_live(operator)
    counts = copy.deepcopy(server[2]())
    for _ in range(2):
        assert (
            get(client, repo, f"/repositories/{repo}/a2a-submissions", key="operator-live").json()
            == live
        )
        assert (
            post(client, repo, f"/repositories/{repo}/a2a-runs", request, "operator-live").json()
            == live
        )
    assert server[2]() == counts
    assert source["adapter"]["actual_attempts"] == 1
    assert source["adapter"]["recording_eligibility"] == "requires_verification"
    spec = source["adapter"]["recording_candidate"]
    branch = post(
        client,
        repo,
        "/branches",
        {
            "schema_version": 1,
            "source_run_id": live["run_id"],
            "from_commit": spec["from_commit"],
            "name": "recorded",
            "expected_version": 0,
        },
        "fork",
    )
    assert branch.status_code == 201, branch.text
    replay = dict(request, branch_name="recorded", expected_head=spec["from_commit"], replay=spec)
    response = post(client, repo, f"/repositories/{repo}/a2a-runs", replay, "recorded")
    assert response.status_code == 202, response.text
    target = response.json()
    for _ in range(4):
        result = work(manager)
        if result["status"] == "completed":
            break
        assert result["status"] == "queued"
    else:
        pytest.fail("Recorded未在完整范围内完成")
    current = get(client, repo, f"/runs/{target['run_id']}").json()["adapter"]
    assert current["logical_state_ref"] == source["adapter"]["logical_state_ref"]
    assert current["physical_state_ref"] != source["adapter"]["physical_state_ref"]
    assert current["remote_identity_scope"] == "historical_source"
    assert current["task_id"] == source["adapter"]["task_id"]
    assert current["actual_attempts"] == current["actual_response_bytes"] == 0
    assert server[2]() == counts
    events = get(client, repo, f"/runs/{target['run_id']}/events").json()["items"]
    assert len([e for e in events if e["type"] == "call.recorded"]) == 1
    artifacts = get(client, repo, f"/runs/{target['run_id']}/artifacts").json()["items"]
    assert len(artifacts) == 2
    for item in artifacts:
        assert get(client, repo, f"/artifacts/{item['id']}/content").status_code == 200


@pytest.mark.parametrize(
    "operation",
    [
        "run",
        "job",
        "events",
        "approvals",
        "control",
        "program",
        "commit",
        "commit_artifacts",
        "artifact",
        "diff",
        "fork",
        "executions",
        "behavior",
        "submission",
        "decision",
    ],
)
@pytest.mark.parametrize("boundary", ["actor", "project"])
def test_other_operator_cannot_bypass_a2a_scope_through_common_routes(
    operator, operation, boundary
):
    client, manager, repo, _, _ = operator
    receipt, source, _ = finish_live(operator)
    artifact = get(client, repo, f"/runs/{receipt['run_id']}/artifacts").json()["items"][0]["id"]
    approval = get(client, repo, "/approvals", run_id=receipt["run_id"]).json()["items"][0]
    other = ManagementService(
        manager.research, actor="other-operator" if boundary == "actor" else ACTOR
    )
    requested_repo = (
        repo if boundary == "actor" else manager.history.create_repository("另一个项目")
    )
    with api_for(other) as unauthorized:
        path = {
            "run": f"/runs/{receipt['run_id']}",
            "job": f"/jobs/{receipt['job_id']}",
            "events": f"/runs/{receipt['run_id']}/events",
            "approvals": "/approvals",
            "program": f"/repositories/{repo}/a2a-runs/{receipt['run_id']}/program",
            "commit": f"/commits/{source['checkpoint_commit_id']}",
            "commit_artifacts": f"/commits/{source['checkpoint_commit_id']}/artifacts",
            "artifact": f"/artifacts/{artifact}/content",
            "diff": "/diff",
            "executions": f"/runs/{receipt['run_id']}/executions",
            "behavior": f"/runs/{receipt['run_id']}/behavioral-trace",
            "submission": f"/repositories/{repo}/a2a-submissions",
        }
        if operation == "control":
            response = post(
                unauthorized,
                requested_repo,
                f"/runs/{receipt['run_id']}/cancel",
                {"schema_version": 1, "expected_version": source["state_version"]},
                "foreign-control",
            )
        elif operation == "fork":
            response = post(
                unauthorized,
                requested_repo,
                "/branches",
                {
                    "schema_version": 1,
                    "source_run_id": receipt["run_id"],
                    "from_commit": source["checkpoint_commit_id"],
                    "name": "foreign",
                    "expected_version": 0,
                },
                "foreign-fork",
            )
        elif operation == "decision":
            response = post(
                unauthorized,
                requested_repo,
                f"/approvals/{approval['id']}/decision",
                {
                    "schema_version": 1,
                    "decision": "approve",
                    "expected_version": source["state_version"],
                },
                "foreign-decision",
            )
        else:
            params = (
                {"run_id": receipt["run_id"]}
                if operation == "approvals"
                else {
                    "left_commit": source["checkpoint_commit_id"],
                    "right_commit": source["checkpoint_commit_id"],
                }
                if operation == "diff"
                else {"key": "operator-live"}
                if operation == "submission"
                else {}
            )
            response = get(unauthorized, requested_repo, path[operation], **params)
        assert response.status_code == (
            404 if operation == "submission" or boundary == "project" else 403
        ), response.text
        assert "task_json" not in response.text and "card_json" not in response.text


def test_a2a_and_generic_pagination_filter_before_page_and_reject_foreign_cursor(operator):
    client, manager, repo, _, _ = operator
    receipt, _ = submit(operator)
    other = ManagementService(manager.research, actor="other-operator")
    with api_for(other) as foreign:
        for path in (f"/repositories/{repo}/a2a-runs", f"/repositories/{repo}/runs"):
            assert get(foreign, repo, path, limit=1).json()["items"] == []
            assert get(foreign, repo, path, after_id=receipt["run_id"]).status_code == 404
    assert len(get(client, repo, f"/repositories/{repo}/a2a-runs", limit=1).json()["items"]) == 1


def test_protocol_discovery_does_not_read_or_send_worker_credentials(operator, monkeypatch):
    client, _, _, program, server = operator
    monkeypatch.delenv(program.binding.server.credential_env)
    response = client.post("/a2a/bind", json=program.binding.server.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert all(r["actor"] is None for r in server[2]()["requests"])


@pytest.mark.parametrize(
    "endpoint", ["http://localhost:1234", "http://192.168.0.2:8000", "http://127.0.0.1:1234/path"]
)
def test_public_bind_rejects_unregistered_address_before_client_creation(
    operator, monkeypatch, endpoint
):
    client, _, _, program, server = operator
    before = copy.deepcopy(server[2]())

    def forbidden(*args, **kwargs):
        pytest.fail("地址拒绝前不应构造客户端")

    monkeypatch.setattr("agentgit.services.management.A2AClient", forbidden)
    invalid = program.binding.server.model_dump(mode="json")
    invalid["endpoint"] = endpoint
    assert client.post("/a2a/bind", json=invalid).status_code == 422
    assert server[2]() == before


def test_public_discovery_never_follows_redirect(operator):
    client, _, _, program, server = operator
    server[1]("redirect")
    before = len(server[2]()["requests"])
    response = client.post("/a2a/bind", json=program.binding.server.model_dump(mode="json"))
    assert response.status_code == 409 and response.json()["error"]["code"] == "unsupported_schema"
    assert len(server[2]()["requests"]) == before + 1


@pytest.mark.parametrize("mode", ["disconnect_after_accept", "working"])
def test_unknown_and_separate_remote_cancel_are_truthful(operator, mode):
    client, manager, repo, _, server = operator
    server[1](mode)
    receipt, _ = submit(operator)
    assert work(manager)["status"] == "waiting_approval"
    approve(client, manager, repo, receipt)
    result = work(manager)
    if mode == "disconnect_after_accept":
        assert result["status"] == "outcome_unknown"
        view = get(client, repo, f"/runs/{receipt['run_id']}").json()["adapter"]
        assert view["actual_attempts"] is None and view["registered_attempts"] == 1
        counts = copy.deepcopy(server[2]())
        assert (
            get(client, repo, f"/repositories/{repo}/a2a-submissions", key="operator-live").json()
            == receipt
        )
        assert server[2]() == counts
        return
    assert result["status"] == "queued"
    view = get(client, repo, f"/runs/{receipt['run_id']}").json()
    assert (
        post(
            client,
            repo,
            f"/runs/{receipt['run_id']}/cancel",
            {"schema_version": 1, "expected_version": view["state_version"]},
            "cancel",
        ).status_code
        == 202
    )
    assert work(manager)["status"] == "waiting_approval"
    assert (
        get(client, repo, f"/runs/{receipt['run_id']}").json()["adapter"]["remote_cancel_status"]
        == "pending_approval"
    )
    approve(client, manager, repo, receipt, "approve-cancel")
    assert work(manager)["status"] == "cancelled"
    assert (
        get(client, repo, f"/runs/{receipt['run_id']}").json()["adapter"]["remote_cancel_status"]
        == "confirmed"
    )


def test_input_secrets_and_wrong_project_never_publish_a_program(operator):
    client, manager, repo, program, _ = operator
    before = {p: p.read_bytes() for p in manager.history.objects.root.rglob("*") if p.is_file()}
    request = NewA2ARun(program=program, expected_head=None, expected_version=0).model_dump(
        mode="json"
    )
    body = json.loads(request["program"]["messages"][0])
    body["message"]["parts"][0]["text"] = os.environ[program.binding.server.credential_env]
    request["program"]["messages"] = [encode(body).decode()]
    response = post(client, repo, f"/repositories/{repo}/a2a-runs", request, "secret")
    assert response.status_code == 403
    if body["message"]["parts"][0]["text"] in response.text:
        pytest.fail("错误响应泄露运行时凭据")
    assert (
        post(
            client, uuid4(), f"/repositories/{repo}/a2a-runs", request, "wrong-project"
        ).status_code
        == 404
    )
    after = {p: p.read_bytes() for p in manager.history.objects.root.rglob("*") if p.is_file()}
    if before != after:
        pytest.fail("拒绝输入前后物理CAS发生变化")


def test_cli_uses_same_submit_confirmation_status_and_approval_services(
    operator, monkeypatch, tmp_path
):
    _, manager, repo, program, _ = operator
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    file = tmp_path / "program.json"
    file.write_bytes(encode(program.model_dump(mode="json")))

    def invoke(args):
        response = CliRunner().invoke(app, args)
        assert response.exit_code == 0, (response.stdout, response.exception)
        return json.loads(response.stdout)

    receipt = invoke(
        ["a2a-submit", str(repo), str(file), "--expected-version", "0", "--key", "cli-live"]
    )
    assert invoke(["a2a-submission", str(repo), "--key", "cli-live"]) == receipt
    assert invoke(["a2a-list", str(repo)])["items"][0]["id"] == receipt["run_id"]
    assert invoke(["worker", "--once"])["status"] == "waiting_approval"
    approval = invoke(["approvals", str(repo), receipt["run_id"]])["items"][0]
    result = CliRunner().invoke(
        app,
        [
            "decision",
            str(repo),
            approval["id"],
            "approve",
            "--expected-version",
            str(approval["version"]),
            "--key",
            "cli-approve",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.exception)
    assert invoke(["worker", "--once"])["status"] == "completed"
    assert invoke(["status", str(repo), receipt["run_id"]])["adapter"]["actual_attempts"] == 1
    assert invoke(["a2a-program", str(repo), receipt["run_id"]]) == program.model_dump(mode="json")


def test_generic_recorded_replay_cannot_take_a2a_job(operator):
    client, manager, repo, _, server = operator
    receipt, source, _ = finish_live(operator)
    spec = source["adapter"]["recording_candidate"]
    before = copy.deepcopy(server[2]())
    response = post(
        client,
        repo,
        "/replays",
        dict(
            schema_version=1,
            mode="recorded",
            source_run_id=receipt["run_id"],
            from_commit=spec["from_commit"],
            until_commit=spec["to_commit"],
            branch_name="wrong-generic",
            expected_version=0,
        ),
        "wrong-generic",
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "unsupported_schema"
    assert server[2]() == before
    with manager.history.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one() == 1


def test_recorded_candidate_still_rejects_missing_source_record_on_submit(operator):
    client, manager, repo, _, server = operator
    receipt, source, request = finish_live(operator)
    spec = source["adapter"]["recording_candidate"]
    response = post(
        client,
        repo,
        "/branches",
        dict(
            schema_version=1,
            source_run_id=receipt["run_id"],
            from_commit=spec["from_commit"],
            name="missing",
            expected_version=0,
        ),
        "missing-fork",
    )
    assert response.status_code == 201
    calls = TABLES["external_calls"]
    with manager.history.engine.begin() as c:
        row = c.execute(sa.select(calls)).mappings().one()
        c.execute(calls.update().where(calls.c.id == row["id"]).values(result_ref=None))
    before = copy.deepcopy(server[2]())
    try:
        response = post(
            client,
            repo,
            f"/repositories/{repo}/a2a-runs",
            dict(request, branch_name="missing", expected_head=spec["from_commit"], replay=spec),
            "missing",
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == "replay_mismatch"
        assert server[2]() == before
        with manager.history.engine.connect() as c:
            assert (
                c.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one() == 1
            )
    finally:
        with manager.history.engine.begin() as c:
            c.execute(
                calls.update().where(calls.c.id == row["id"]).values(result_ref=row["result_ref"])
            )


def test_public_recorded_cancel_only_stops_local_playback(operator):
    client, manager, repo, _, server = operator
    receipt, source, request = finish_live(operator)
    spec = source["adapter"]["recording_candidate"]
    assert (
        post(
            client,
            repo,
            "/branches",
            dict(
                schema_version=1,
                source_run_id=receipt["run_id"],
                from_commit=spec["from_commit"],
                name="local-cancel",
                expected_version=0,
            ),
            "local-fork",
        ).status_code
        == 201
    )
    target = post(
        client,
        repo,
        f"/repositories/{repo}/a2a-runs",
        dict(request, branch_name="local-cancel", expected_head=spec["from_commit"], replay=spec),
        "local-recorded",
    ).json()
    before = copy.deepcopy(server[2]())
    assert (
        post(
            client,
            repo,
            f"/runs/{target['run_id']}/cancel",
            dict(schema_version=1, expected_version=0),
            "local-cancel",
        ).status_code
        == 202
    )
    assert work(manager)["status"] == "cancelled"
    view = get(client, repo, f"/runs/{target['run_id']}").json()["adapter"]
    assert (
        view["control_scope"] == "local_recorded"
        and view["remote_cancel_status"] == "not_requested"
    )
    assert view["actual_attempts"] == 0 and server[2]() == before
