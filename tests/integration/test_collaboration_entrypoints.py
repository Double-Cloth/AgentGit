"""协作 HTTP/CLI 共用实际持久身份，查询不暴露角色私有输入或启动执行。"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, rows
from typer.testing import CliRunner

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "collaboration-operator"
TOKEN = "fixture-collaboration-api-token-0000000000000"


def client_for(service, repo):
    return TestClient(
        create_app(
            Settings(api_token=SecretStr(TOKEN), operator_id=service.actor), management=service
        ),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    )


@pytest.fixture
def case(history, tmp_path):
    service = ManagementService(ResearchService(history, tmp_path / "d"), actor=ACTOR)
    repo = history.create_repository("操作者四角色协作")
    body = json.loads(Path("examples/collaboration/research.json").read_text(encoding="utf-8"))
    body["private_inputs"] = [
        {"schema_version": 1, "role": r, "notes": ["private-" + r]}
        for r in ("supervisor", "researcher", "verifier", "writer")
    ]
    with client_for(service, repo) as client:
        yield client, service, repo, body


def post(client, path, body=None, key="once"):
    return client.post(
        path,
        json=body if body is not None else {"schema_version": 1},
        headers={"Idempotency-Key": key},
    )


def prepare(case):
    client, _, repo, body = case
    response = post(client, f"/repositories/{repo}/collaborations/prepare", body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    return receipt, f"/repositories/{repo}/collaborations/{receipt['collaboration_id']}"


def counts(service):
    with service.history.engine.connect() as c:
        return {
            n: c.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
            for n, t in TABLES.items()
        }


def proof(request, **value):
    root = os.environ.get("AGENTGIT_COLLABORATION_OPERATOR_PROOF_DIR")
    if root:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
        with (path / name).open("x", encoding="utf-8") as f:
            json.dump(dict(test=request.node.nodeid, **value), f, ensure_ascii=False, indent=2)


def pages(client, path, *, shared=False):
    items = []
    params = {"limit": 2}
    while True:
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        data = response.json()
        items.extend(data["items"])
        cursor = data["next_after"] if shared else data["next_after_id"]
        if cursor is None:
            return items
        params = {
            "limit": 2,
            **(
                {"after_path": cursor["path"], "after_version": cursor["version"]}
                if shared
                else {"after_id": cursor}
            ),
        }


def test_prepare_is_idempotent_and_requires_explicit_start(case, request):
    client, service, repo, body = case
    receipt, path = prepare(case)
    before = counts(service)
    assert post(client, f"/repositories/{repo}/collaborations/prepare", body).json() == receipt
    assert Worker(service.jobs).run_once() is None and counts(service) == before
    summary = client.get(path).json()
    assert summary["status"] == "prepared" and summary["quiescent"] and len(summary["roles"]) == 4
    views = [client.get(f"/runs/{r['run_id']}").json() for r in summary["roles"]]
    assert all(
        v["adapter"]["phase"] == "prepared" and v["adapter"]["physical_call_intents"] == 0
        for v in views
    )
    assert all(
        "private-" + r not in json.dumps([summary, views])
        for r in ("supervisor", "researcher", "verifier", "writer")
    )
    assert (
        client.get(f"/repositories/{repo}/collaborations").json()["items"][0]["collaboration_id"]
        == receipt["collaboration_id"]
    )
    assert counts(service) == before
    proof(
        request, receipt=receipt, summary=summary, views=views, before=before, after=counts(service)
    )


def test_http_live_flow_public_transport_and_reopen_are_read_only(case, monkeypatch, request):
    client, service, repo, _ = case
    receipt, path = prepare(case)
    start = post(client, path + "/start", key="start")
    assert start.status_code == 202, start.text
    drain(service.jobs)
    summary = client.get(path).json()
    assert summary["status"] == "completed"
    before = counts(service)
    views = []
    for r in summary["roles"]:
        response = client.get(f"/runs/{r['run_id']}")
        assert response.status_code == 200, response.text
        view = response.json()
        views.append(view)
        for endpoint in (
            f"/runs/{r['run_id']}/events",
            f"/runs/{r['run_id']}/artifacts",
            f"/commits/{view['checkpoint_commit_id']}",
        ):
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
    assert sum(v["adapter"]["physical_call_intents"] for v in views) == 20
    messages = pages(client, path + "/messages")
    shared = pages(client, path + "/shared", shared=True)
    assert len(messages) == 7 and len({m["message_id"] for m in messages}) == 7
    assert all(
        m["received_seq"] and m["parent_event_id"]["agent_run_id"] == m["sender"] for m in messages
    )
    assert len(shared) == 5 and [s["version"] for s in shared if s["path"] == "evidence.json"] == [
        1,
        2,
    ]
    writer = next(r for r in summary["roles"] if r["role"] == "writer")
    artifacts = client.get(f"/runs/{writer['run_id']}/artifacts").json()["items"]
    report = next(a for a in artifacts if a["relative_path"] == "report.md")
    response = client.get(f"/artifacts/{report['id']}/content")
    assert response.status_code == 200 and "Aster" in response.text
    writer_view = next(v for v in views if v["id"] == writer["run_id"])
    checkpoint = writer_view["checkpoint_commit_id"]
    assert (
        client.get(
            "/diff", params={"left_commit": checkpoint, "right_commit": checkpoint}
        ).status_code
        == 200
    )
    other = ManagementService(service.research, actor="other-operator")

    def forbidden(*args, **kwargs):
        raise AssertionError("其他操作者的角色历史与产物必须先拒绝")

    with client_for(other, repo) as other_client, monkeypatch.context() as selected:
        selected.setattr(service.history.objects, "get", forbidden)
        for endpoint in (
            f"/commits/{writer_view['checkpoint_commit_id']}",
            f"/commits/{writer_view['checkpoint_commit_id']}/artifacts",
            f"/artifacts/{report['id']}/content",
            f"/jobs/{writer['job_id']}",
            f"/diff?left_commit={checkpoint}&right_commit={checkpoint}",
            f"/repositories/{repo}/commits?branch_name={writer_view['branch_name']}",
            f"/runs/{writer['run_id']}/behavioral-trace",
            f"/behavioral-diff?left_run_id={writer['run_id']}&right_run_id={writer['run_id']}",
            f"/runs/{writer['run_id']}/executions",
        ):
            response = other_client.get(endpoint)
            assert response.status_code == 403, response.text
    with client_for(service, repo) as reopened:
        assert reopened.get(path).json() == summary
        assert post(reopened, path + "/start", key="start").json() == start.json()
    assert counts(service) == before and service.history.branch(repo)["head_commit_id"] is None
    proof(
        request,
        receipt=receipt,
        views=views,
        messages=messages,
        shared=shared,
        before=before,
        after=counts(service),
    )


def test_http_recorded_flow_reuses_only_own_origin_without_clients(case, monkeypatch, request):
    client, service, _, _ = case
    source, source_path = prepare(case)
    assert post(client, source_path + "/start", key="start").status_code == 202
    drain(service.jobs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded 入口和 Worker 不得构造执行客户端")

    with monkeypatch.context() as selected:
        for cls in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
            selected.setattr(cls, "__init__", forbidden)
        selected.setattr(subprocess, "Popen", forbidden)
        response = post(client, source_path + "/recorded", key="recorded")
        assert response.status_code == 201, response.text
        target = response.json()
        path = source_path.rsplit("/", 1)[0] + "/" + target["collaboration_id"]
        assert post(client, source_path + "/recorded", key="recorded").json() == target
        assert client.get(path).json()["status"] == "prepared"
        assert post(client, path + "/start", key="recorded-start").status_code == 202
        drain(service.jobs)
        summary = client.get(path).json()
        assert summary["status"] == "completed"
        views = [client.get(f"/runs/{r['run_id']}").json() for r in summary["roles"]]
        messages = pages(client, path + "/messages")
        shared = pages(client, path + "/shared", shared=True)
    assert sum(v["adapter"]["physical_call_intents"] for v in views) == 0
    assert sum(v["adapter"]["reused_calls"] for v in views) == 20
    assert summary["source_collaboration_id"] == source["collaboration_id"]
    assert (
        len(messages) == 7 and len(shared) == 5 and len(rows(service.jobs, "external_calls")) == 40
    )
    proof(
        request,
        source=source,
        target=target,
        views=views,
        messages=messages,
        shared=shared,
        actual_clients=0,
    )


@pytest.mark.parametrize(
    "operation",
    [
        "status",
        "messages",
        "shared",
        "start",
        "control",
        "recorded",
        "run",
        "events",
        "artifacts",
        "run-control",
    ],
)
def test_other_actor_is_rejected_before_cas(case, operation, monkeypatch, request):
    _, service, repo, _ = case
    receipt, path = prepare(case)
    other = ManagementService(service.research, actor="other-operator")
    run = receipt["supervisor_run_id"]
    before = counts(service)

    def forbidden(*args, **kwargs):
        raise AssertionError("其他操作者必须在 CAS 读取前拒绝")

    with (
        client_for(other, repo) as client,
        monkeypatch.context() as selected,
    ):
        selected.setattr(service.history.objects, "get", forbidden)
        if operation == "run-control":
            response = post(
                client, f"/runs/{run}/cancel", {"schema_version": 1, "expected_version": 0}
            )
        elif operation in ("start", "recorded", "control"):
            response = post(
                client,
                path + "/" + operation,
                {"schema_version": 1, "action": "cancel", "expected_revision": 0}
                if operation == "control"
                else None,
            )
        elif operation in ("run", "events", "artifacts"):
            response = client.get(f"/runs/{run}" + ("/" + operation if operation != "run" else ""))
        else:
            response = client.get(path + ("/" + operation if operation != "status" else ""))
        assert response.status_code == 403, response.text
        assert client.get(f"/repositories/{repo}/collaborations").json()["items"] == []
    assert counts(service) == before
    proof(
        request, operation=operation, denied=response.json(), before=before, after=counts(service)
    )


@pytest.mark.parametrize(
    "operation", ["status", "messages", "shared", "start", "control", "recorded"]
)
def test_wrong_project_and_mismatched_header_never_read_cas(case, operation, monkeypatch):
    client, service, repo, _ = case
    receipt, path = prepare(case)
    other = service.history.create_repository("其他协作项目")

    def forbidden(*args, **kwargs):
        raise AssertionError("跨项目请求必须先拒绝")

    suffix = "" if operation == "status" else "/" + operation
    with monkeypatch.context() as selected:
        selected.setattr(service.history.objects, "get", forbidden)
        for selected_path in (path, path.replace(str(repo), str(other))):
            headers = {"X-Repository-ID": str(other), "Idempotency-Key": "cross-project"}
            if operation in ("start", "control", "recorded"):
                body = (
                    {"schema_version": 1, "action": "cancel", "expected_revision": 0}
                    if operation == "control"
                    else {"schema_version": 1}
                )
                response = client.post(selected_path + suffix, json=body, headers=headers)
            else:
                response = client.get(selected_path + suffix, headers=headers)
            expected = 403 if operation == "recorded" and selected_path != path else 404
            assert response.status_code == expected, response.text


def test_http_boundaries_versions_and_prepared_cancel(case, request):
    client, service, repo, body = case
    receipt, path = prepare(case)
    for endpoint, params in (
        (path + "/messages", {"limit": 101}),
        (path + "/shared", {"after_path": "evidence.json"}),
        (path + "/shared", {"after_version": 1}),
        (path + "/shared", {"after_path": "../private", "after_version": 1}),
        (f"/repositories/{repo}/collaborations", {"limit": 0}),
    ):
        assert client.get(endpoint, params=params).status_code == 422
    assert post(client, path + "/start", {"schema_version": 1, "actor": ACTOR}).status_code == 422
    assert client.post(path + "/start", json={"schema_version": 1}).status_code == 422
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert (
        post(
            client,
            path + "/control",
            {"schema_version": 1, "action": "cancel", "expected_revision": 99},
        ).status_code
        == 409
    )
    before = counts(service)
    changed = json.loads(json.dumps(body))
    changed["program"]["goal"]["text"] += "不同输入"
    assert post(client, f"/repositories/{repo}/collaborations/prepare", changed).status_code == 409
    assert counts(service) == before
    cancellation = {"schema_version": 1, "action": "cancel", "expected_revision": 0}
    response = post(client, path + "/control", cancellation, key="cancel")
    assert response.status_code == 202 and response.json()["decision"] == "accepted"
    assert post(client, path + "/control", cancellation, key="cancel").json() == response.json()
    assert not client.get(path).json()["all_terminal"]
    drain(service.jobs)
    assert client.get(path).json()["all_terminal"] and not rows(service.jobs, "external_calls")
    assert (
        client.post(
            f"/repositories/{repo}/collaborations/prepare",
            content=b" " * 1_048_577,
            headers={"Content-Type": "application/json", "Idempotency-Key": "oversize"},
        ).status_code
        == 413
    )
    proof(request, receipt=receipt, cancellation=response.json(), status=client.get(path).json())


def test_cli_invalid_cursor_and_oversized_file_are_bounded(case, monkeypatch, tmp_path):
    _, service, repo, _ = case
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    runner = CliRunner()
    before = counts(service)
    result = runner.invoke(
        app,
        [
            "collaboration-shared",
            str(repo),
            str(repo),
            "--after-path",
            "evidence.json",
            "--after-version",
            "9007199254740992",
            "--testing",
        ],
    )
    assert result.exit_code != 0 and json.loads(result.output)["error"]["code"] == "validation"
    file = tmp_path / "oversized.json"
    file.write_bytes(b" " * 1_048_577)
    result = runner.invoke(
        app, ["collaboration-prepare", str(repo), str(file), "--key", "large", "--testing"]
    )
    assert result.exit_code != 0 and json.loads(result.output)["error"]["code"] == "output_limit"
    assert counts(service) == before


def test_cli_live_control_recorded_and_queries_share_http_identities(
    case, monkeypatch, tmp_path, request
):
    client, service, repo, body = case
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    runner = CliRunner()
    file = tmp_path / "program.json"
    file.write_bytes(encode(body))

    def cli(command, *args):
        result = runner.invoke(app, [command, str(repo), *map(str, args), "--testing"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    receipt = cli("collaboration-prepare", file, "--key", "cli-prepare")
    assert cli("collaboration-prepare", file, "--key", "cli-prepare") == receipt
    group = receipt["collaboration_id"]
    path = f"/repositories/{repo}/collaborations/{group}"
    assert cli("collaboration-list")["items"][0]["collaboration_id"] == group
    assert cli("collaboration-status", group) == client.get(path).json()
    cli("collaboration-start", group, "--key", "cli-start")
    assert Worker(service.jobs).run_once()["status"] == "paused"
    paused = []

    def pause(point):
        if point == "collaboration.call_reserved" and not paused:
            response = post(
                client,
                path + "/control",
                {"schema_version": 1, "action": "pause", "expected_revision": 0},
                key="http-pause",
            )
            assert response.status_code == 202, response.text
            paused.append(response.json())

    assert Worker(service.jobs, fault=pause).run_once()["status"] == "paused"
    drain(service.jobs)
    assert cli("collaboration-status", group)["quiescent"]
    control = tmp_path / "resume.json"
    control.write_bytes(encode(dict(schema_version=1, action="resume", expected_revision=1)))
    resumed = cli("collaboration-control", group, control, "--key", "cli-resume")
    assert resumed["decision"] == "accepted"
    drain(service.jobs)
    assert cli("collaboration-status", group)["status"] == "completed"
    assert len(cli("collaboration-messages", group)["items"]) == 7
    assert len(cli("collaboration-shared", group)["items"]) == 5
    recorded = cli("collaboration-recorded", group, "--key", "cli-recorded")
    assert cli("collaboration-status", recorded["collaboration_id"])["status"] == "prepared"
    cli("collaboration-start", recorded["collaboration_id"], "--key", "recorded-start")
    drain(service.jobs)
    assert cli("collaboration-status", recorded["collaboration_id"])["status"] == "completed"
    first = cli("collaboration-list", "--limit", "1")
    assert first["next_after_id"] is not None and len(first["items"]) == 1
    second = cli("collaboration-list", "--limit", "1", "--after-id", first["next_after_id"])
    assert second["next_after_id"] is None and len(second["items"]) == 1
    assert {r["collaboration_id"] for r in first["items"] + second["items"]} == {
        group,
        recorded["collaboration_id"],
    }
    before = counts(service)
    cli("collaboration-status", group)
    cli("collaboration-shared", group)
    assert counts(service) == before
    proof(
        request,
        source=receipt,
        recorded=recorded,
        pause=paused[0],
        resume=resumed,
        before=before,
        after=counts(service),
        group_pages=[first, second],
    )
