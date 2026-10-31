"""真实 PostgreSQL 网关：精确审批、耐久调用去重与已有 Recorded 字节消费。"""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_a2a_fixture import server as server
from test_a2a_transport import operation, sending, setup

from agentgit.adapters.a2a import A2AAdapter
from agentgit.core.a2a_wire import A2AResult
from agentgit.core.canonical import ObjectType
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, EnvironmentManifest, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.replay.recorded import RecordedAdapter, Recording
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, EventInput

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def archive(history, server, request):
    yield
    destination = os.environ.get("AGENTGIT_A2A_GATEWAY_PROOF_DIR")
    if destination is None:
        return
    names = (
        "objects",
        "object_refs",
        "repositories",
        "runs",
        "branches",
        "commits",
        "events",
        "external_calls",
        "approvals",
    )
    with history.engine.connect() as connection:
        tables = {
            name: [dict(r) for r in connection.execute(sa.select(TABLES[name])).mappings()]
            for name in names
        }
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
        migration = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    objects = []
    for row in tables["objects"]:
        raw = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            dict(
                id=row["id"],
                type=row["object_type"],
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                base64=base64.b64encode(raw).decode(),
            )
        )
    packet = dict(
        kind="a2a_gateway_raw/v1",
        test=request.node.nodeid,
        source=source_revision(),
        lock=dependency_lock_digest(),
        schema=schema,
        migration=migration,
        server=server[2](),
        tables=tables,
        objects=objects,
    )
    path = Path(destination)
    path.mkdir(parents=True, exist_ok=True)
    with (path / (hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json")).open(
        "x", encoding="utf-8"
    ) as stream:
        json.dump(packet, stream, default=str, ensure_ascii=False, indent=2)


def prepared(history, server):
    client, binding, mode, state, observed = setup(server)
    adapter = A2AAdapter(binding, client)
    repo = history.create_repository("A2A 精确动作")
    run = history.create_run(repo)
    environment = EnvironmentManifest(
        runtime_version="0.1.0",
        python_version="3.12",
        code_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        model_signature="no-model",
        prompt_digest="b" * 64,
        input_ref=history.register_input(repo, b"public-a2a-fixture"),
        evaluation_time="2026-10-31T00:00:00Z",
        random_seed=0,
    )
    history.commit(
        repo,
        run,
        expected_head=None,
        expected_version=0,
        state={"fixture": "before-a2a"},
        environment=environment,
        events=(EventInput("fixture.ready", "a2a", {}),),
    )
    approval = ApprovalService(
        history,
        ToolPolicy(rules=tuple(binding.rule(op) for op in ("send", "get", "cancel"))),
        actor="a2a-operator",
    )

    def checkpoint():
        branch = history.branch(repo)
        return branch["head_commit_id"], branch["version"]

    gateway = RecordedCallGateway(history, repo, run, checkpoint, authorization=approval)
    return adapter, gateway, approval, repo, run, mode, state, observed


def approve(history, values, call, step):
    adapter, gateway, approval, repo, run, _, state, _ = values
    before = len(state()["requests"])
    with pytest.raises(AgentGitError) as error:
        adapter.call(gateway, step=step, attempt=1, call=call)
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED and len(state()["requests"]) == before
    pending = approval.list_for_run(repo, run)[-1]
    with history.engine.connect() as connection:
        version = history._run(connection, repo, run)["state_version"]
    approval.decide(
        repo, pending["id"], actor="a2a-operator", decision="approve", expected_version=version
    )


def test_gateway_requires_exact_approval_and_cached_success_has_no_request(
    history, server, monkeypatch
):
    values = prepared(history, server)
    adapter, gateway, approval, repo, run, _, state, _ = values
    call = sending()
    approve(history, values, call, "send")
    outcome = adapter.call(gateway, step="send", attempt=1, call=call)
    assert outcome.error is None and state()["accepted_messages"] == 1
    before = len(state()["requests"])
    assert adapter.call(gateway, step="send", attempt=1, call=call) == outcome
    assert (
        len(state()["requests"]) == before
        and approval.list_for_run(repo, run)[-1]["status"] == "consumed"
    )
    with history.engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent)
    saved = parse_contract(
        history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
    )
    record = Recording(
        intent,
        saved,
        history.objects.get(intent.request_ref, ObjectType.BLOB),
        history.objects.get(saved.response_ref, ObjectType.BLOB),
        str(row["id"]),
    )
    replay = RecordedAdapter((record,), lambda: intent.checkpoint)
    offline = A2AAdapter(adapter.binding)
    # 使用已有严格 Recorded 网关，不构造任何客户端；尚非新的完整 A2A Recorded Run。
    monkeypatch.setattr(
        "agentgit.adapters.a2a.request", lambda *a, **kw: pytest.fail("Recorded 不得创建请求")
    )
    replayed = offline.call(replay, step="send", attempt=1, call=call)
    replay.assert_consumed()
    assert replayed.payload == outcome.payload and len(state()["requests"]) == before
    with pytest.raises(AgentGitError) as missing:
        offline.call(
            RecordedAdapter((), lambda: intent.checkpoint), step="send", attempt=1, call=call
        )
    assert missing.value.code == ErrorCode.RECORDING_MISSING


@pytest.mark.parametrize(
    "mode_name", ["disconnect_after_accept", "server_error_after_accept", "echo_credential"]
)
def test_durable_gateway_never_reissues_unknown_write(history, server, mode_name):
    values = prepared(history, server)
    adapter, gateway, _, _, _, mode, state, observed = values
    call = sending()
    approve(history, values, call, "send")
    mode(mode_name)
    outcome = adapter.call(gateway, step="send", attempt=1, call=call)
    assert outcome.error.code == ErrorCode.OUTCOME_UNKNOWN and state()["accepted_messages"] == 1
    before = len(state()["requests"])
    with pytest.raises(AgentGitError) as repeated:
        adapter.call(gateway, step="send", attempt=1, call=call)
    assert repeated.value.code == ErrorCode.OUTCOME_UNKNOWN and len(state()["requests"]) == before
    secret = os.environ[observed["credential_envs"]["owner"]].encode()
    with history.engine.connect() as connection:
        objects = connection.execute(sa.select(TABLES["objects"])).mappings().all()
    assert all(
        secret not in history.objects.get(row["id"], ObjectType(row["object_type"]))
        for row in objects
    )


def test_query_is_readonly_but_cancel_requires_separate_exact_approval(history, server):
    values = prepared(history, server)
    adapter, gateway, _, _, _, mode, state, _ = values
    mode("working")
    approve(history, values, sending(), "send")
    result = parse_contract(
        adapter.call(gateway, step="send", attempt=1, call=sending()).payload, A2AResult
    )
    query = adapter.call(gateway, step="query", attempt=1, call=operation("get", result))
    assert query.error is None
    cancel = operation("cancel", result)
    approve(history, values, cancel, "cancel")
    outcome = adapter.call(gateway, step="cancel", attempt=1, call=cancel)
    assert (
        outcome.error is None
        and parse_contract(outcome.payload, A2AResult).value()["status"]["state"]
        == "TASK_STATE_CANCELED"
    )
    before = len(state()["requests"])
    assert adapter.call(gateway, step="cancel", attempt=1, call=cancel) == outcome
    assert len(state()["requests"]) == before


def test_approval_from_another_repository_does_not_authorize_this_call(history, server):
    first = prepared(history, server)
    second = prepared(history, server)
    approve(history, first, sending(), "send")
    before = len(first[6]()["requests"])
    with pytest.raises(AgentGitError) as error:
        second[0].call(second[1], step="send", attempt=1, call=sending())
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    assert len(first[6]()["requests"]) == before and first[6]()["accepted_messages"] == 0
