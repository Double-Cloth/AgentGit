"""真实四角色 Worker 的消息、私有模型输入、报告和停止传播。"""

import base64
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError
from test_collaboration_preparation import config_for
from test_merge_process_crash import kill_interpreter

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.collaboration import CollaborationProgram, CollaborationRole
from agentgit.core.collaboration_execution import (
    CollaborationRoleState,
    RoleDecision,
    RoleModelRequest,
)
from agentgit.core.collaboration_runs import RolePrivateInput
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import evolve
from agentgit.runtime.collaboration_run import CollaborationRoleRunner
from agentgit.services.collaboration import CollaborationService
from agentgit.services.collaboration_control import CollaborationCoordinator
from agentgit.services.collaboration_exchange import CollaborationExchange
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.management import public
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
PROGRAM = Path(__file__).parents[1] / "fixtures/collaboration/v1/program.json"


@pytest.fixture(autouse=True)
def archive(history, request):
    yield
    destination = os.environ.get("AGENTGIT_COLLABORATION_EXECUTION_EVIDENCE_DIR")
    if not destination:
        return
    with history.engine.connect() as c:
        tables = {
            str(name): [dict(row) for row in c.execute(sa.select(table)).mappings()]
            for name, table in TABLES.items()
        }
        head = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    objects = []
    for row in tables["objects"]:
        raw = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            dict(
                id=row["id"], type=row["object_type"], base64=base64.b64encode(raw).decode("ascii")
            )
        )
    packet = dict(
        kind="collaboration-execution-raw/v1",
        test=request.node.nodeid,
        source_revision=source_revision(),
        lock_digest=dependency_lock_digest(),
        schema_head=head,
        tables=public(tables),
        objects=objects,
    )
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
    with (root / name).open("x", encoding="utf-8") as stream:
        json.dump(packet, stream, ensure_ascii=False, indent=2)
    process = (
        request.node._collaboration_process
        if hasattr(request.node, "_collaboration_process")
        else None
    )
    if process:
        (root / (name.removesuffix(".json") + "-process.json")).write_text(
            json.dumps(process, ensure_ascii=False, indent=2), encoding="utf-8"
        )


@pytest.fixture
def flow(history, tmp_path, request):
    queue = JobService(ResearchService(history, tmp_path / "d"))
    service = CollaborationService(queue)
    repo = history.create_repository("真实四角色 Worker")
    program = parse_contract(PROGRAM.read_bytes(), CollaborationProgram)
    if getattr(request, "param", None) == "supervisor-one-model":
        value = program.model_dump(mode="json")
        value["roles"][0]["limits"]["max_model_calls"] = 1
        program = parse_contract(encode(value), CollaborationProgram)
    variant = getattr(request, "param", None)
    if variant in ("no-researcher-read", "no-writer-write", "writer-one-file"):
        value = program.model_dump(mode="json")
        if variant == "no-researcher-read":
            value["roles"][1]["permissions"].remove("source_read")
        elif variant == "no-writer-write":
            value["roles"][3]["permissions"].remove("artifact_write")
        else:
            value["roles"][3]["limits"]["max_files"] = 1
        program = parse_contract(encode(value), CollaborationProgram)
    private = tuple(
        RolePrivateInput(role=role, notes=("private-" + role.value,)) for role in CollaborationRole
    )
    receipt = service.prepare(
        repo, program, actor="collaboration-operator", key="prepare", private_inputs=private
    )
    coordinator = CollaborationCoordinator(queue)
    summary = service.summary(repo, UUID(receipt.collaboration_id))
    return queue, service, coordinator, repo, receipt, {r["role"]: r for r in summary["roles"]}


def start(flow):
    _, _, coordinator, repo, receipt, _ = flow
    return coordinator.start(
        repo, UUID(receipt.collaboration_id), actor="collaboration-operator", key="start"
    )


def drain(queue, **kwargs):
    results = []
    for _ in range(12):
        result = Worker(queue, **kwargs).run_once()
        if result is None:
            return results
        assert result["status"] != "lease_lost", result
        results.append(result)
    raise AssertionError("固定四角色超过预期领取上限")


def rows(queue, name):
    with queue.history.engine.connect() as c:
        return [dict(r) for r in c.execute(sa.select(TABLES[name])).mappings()]


def state(flow, role):
    queue, _, _, repo, _, roles = flow
    run = queue.run(repo, UUID(roles[role]["run_id"]))
    return parse_contract(
        encode(queue.history.restore(repo, run["checkpoint_commit_id"]).data),
        CollaborationRoleState,
    ), run


def test_real_four_role_flow_has_causal_messages_and_independent_model_contexts(flow):
    queue, service, _, repo, receipt, roles = flow
    main = queue.history.branch(repo)
    started = start(flow)
    assert start(flow) == started
    results = drain(queue)
    assert [r["status"] for r in results] == [
        "paused",
        "completed",
        "completed",
        "completed",
        "completed",
    ], results
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
    assert queue.history.branch(repo) == main
    messages = rows(queue, "collaboration_messages")
    receipts = rows(queue, "collaboration_receipts")
    assert len(messages) == len(receipts) == 7
    assert len(rows(queue, "collaboration_publications")) == 5
    calls = rows(queue, "external_calls")
    assert len(calls) == 20 and sum(c["provider"].startswith("llm:") for c in calls) == 5
    assert {state(flow, role)[0].role.value for role in roles} == set(roles)
    by_run = {r["run_id"]: role for role, r in roles.items()}
    for call in calls:
        assert call["status"] == "succeeded"
        if call["provider"].startswith("llm:"):
            intent = queue.history.objects.get_json(call["intent_ref"], ObjectType.CALL)
            request = parse_contract(
                queue.history.objects.get(intent["request_ref"], ObjectType.BLOB), RoleModelRequest
            )
            role = by_run[str(call["run_id"])]
            assert request.role.value == role and request.private_notes == ("private-" + role,)
            raw = encode(request.model_dump(mode="json"))
            assert all(("private-" + other).encode() not in raw for other in roles if other != role)
    for role in roles:
        current, run = state(flow, role)
        assert (
            current.status == RunStatus.COMPLETED
            and current.pending is None
            and current.usage.reserved_tokens == 0
        )
        assert current.usage.steps == current.usage.model_calls + current.usage.tool_calls
        artifacts = queue.history.restore(repo, run["checkpoint_commit_id"]).artifacts
        assert all(
            b"private-" not in queue.history.objects.get(a.object_ref, ObjectType.BLOB)
            for a in artifacts
        )
    events = {(str(e["run_id"]), e["seq"]): e for e in rows(queue, "events")}
    for ack in receipts:
        message = next(m for m in messages if m["id"] == ack["message_id"])
        event = events[(str(ack["receiver_run_id"]), ack["received_seq"])]
        assert (
            event["caused_by_run_id"] == message["sender_run_id"]
            and event["caused_by_seq"] == message["sent_seq"]
        )
    for name in ("collaboration_messages", "collaboration_receipts", "collaboration_publications"):
        with pytest.raises(DBAPIError), queue.history.engine.begin() as connection:
            connection.execute(TABLES[name].delete())
    with queue.history.engine.begin() as connection:
        with pytest.raises(RuntimeError, match="已有协作消息"):
            command.downgrade(config_for(connection), "0029_collaboration_preparation")
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0030_collaboration_exchange"
        )
        command.upgrade(config_for(connection), "head")


@pytest.mark.parametrize("bad_role", ["researcher", "verifier"])
def test_wrong_role_decision_stops_children_without_writer_calls(flow, bad_role):
    queue, service, _, repo, receipt, roles = flow

    class Bad(MockCollaborationModel):
        def invoke(self, payload):
            request = parse_contract(payload, RoleModelRequest)
            return encode(RoleDecision(role=request.role, action="write").model_dump(mode="json"))

    start(flow)
    drain(
        queue,
        collaboration_model_factory=lambda role: (
            Bad() if role == bad_role else MockCollaborationModel()
        ),
    )
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "failed"
    statuses = {
        role: queue.run(repo, UUID(value["run_id"]))["status"] for role, value in roles.items()
    }
    assert statuses[bad_role] == "failed" and statuses["writer"] == "cancelled"
    assert not any(
        c["run_id"] == UUID(roles["writer"]["run_id"]) for c in rows(queue, "external_calls")
    )
    group = rows(queue, "collaborations")[0]
    assert (
        group["failure_run_id"] == UUID(roles[bad_role]["run_id"])
        and group["failure_event_seq"] > 0
    )
    stopped = [e for e in rows(queue, "events") if e["type"] == "collaboration.parent_stopped"]
    assert stopped and all(
        e["caused_by_run_id"] == group["failure_run_id"]
        and e["caused_by_seq"] == group["failure_event_seq"]
        for e in stopped
    )


def test_pause_after_known_model_result_resumes_without_repeating_call(flow):
    queue, service, _, repo, receipt, roles = flow
    start(flow)
    assert Worker(queue).run_once()["status"] == "paused"
    target = UUID(roles["researcher"]["run_id"])
    paused = []

    def fault(point):
        if point == "collaboration.after_saved_result" and not paused:
            paused.append(True)
            run = queue.run(repo, target)
            queue.request_control(repo, target, "pause", expected_version=run["state_version"])

    assert Worker(queue, fault=fault).run_once()["status"] == "paused"
    assert len([c for c in rows(queue, "external_calls") if c["run_id"] == target]) == 1
    assert Worker(queue).run_once() is None
    run = queue.run(repo, target)
    queue.request_control(repo, target, "resume", expected_version=run["state_version"])
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
    assert len(rows(queue, "external_calls")) == 20


def test_queued_child_cancel_has_no_child_calls_and_causal_group_stop(flow):
    queue, service, _, repo, receipt, roles = flow
    start(flow)
    Worker(queue).run_once()
    target = UUID(roles["researcher"]["run_id"])
    run = queue.run(repo, target)
    queue.request_control(repo, target, "cancel", expected_version=run["state_version"])
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "cancelled"
    assert len(rows(queue, "external_calls")) == 1
    assert all(queue.run(repo, UUID(r["run_id"]))["status"] == "cancelled" for r in roles.values())


def test_duplicate_delivery_uses_durable_receipt_and_own_checkpoint(flow):
    queue, _, _, _, _, _ = flow
    start(flow)
    Worker(queue).run_once()
    lease = queue.claim("receipt-fixture")
    assert lease is not None
    exchange = CollaborationExchange(queue, lease)
    initial = exchange.load()
    exchange.save(initial, "collaboration.initialized")
    once = exchange.receive(initial)
    before = {n: len(rows(queue, n)) for n in ["events", "commits", "collaboration_receipts"]}
    assert exchange.receive(initial) == once and {n: len(rows(queue, n)) for n in before} == before
    exchange.save(evolve(once, status=RunStatus.PAUSED), "collaboration.fixture_paused")
    queue.finish(lease, "paused")


def test_start_requires_creator_identity_and_cannot_restart_completed_group(flow):
    queue, service, coordinator, repo, receipt, _ = flow
    with pytest.raises(AgentGitError) as caught:
        coordinator.start(
            repo, UUID(receipt.collaboration_id), actor="another-operator", key="start"
        )
    assert caught.value.code == ErrorCode.FORBIDDEN and queue.claim("ordinary") is None
    start(flow)
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
    with pytest.raises(AgentGitError) as caught:
        coordinator.start(
            repo, UUID(receipt.collaboration_id), actor="collaboration-operator", key="new-start"
        )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT


@pytest.mark.parametrize("flow", ["supervisor-one-model"], indirect=True)
def test_supervisor_model_ceiling_is_not_expanded_to_finish(flow):
    queue, service, _, repo, receipt, _ = flow
    start(flow)
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "budget_exhausted"
    supervisor, _ = state(flow, "supervisor")
    assert supervisor.usage.model_calls == 1 and supervisor.usage.reserved_tokens == 0
    assert len(rows(queue, "external_calls")) == 19


def test_final_saved_result_is_settled_before_cancel_without_declaring_group_success(flow):
    queue, service, _, repo, receipt, roles = flow
    start(flow)
    cancelled = []
    supervisor = UUID(roles["supervisor"]["run_id"])

    def fault(point):
        if point == "collaboration.after_saved_result" and not cancelled:
            calls = [c for c in rows(queue, "external_calls") if c["run_id"] == supervisor]
            if len(calls) == 2:
                cancelled.append(True)
                current = queue.run(repo, supervisor)
                queue.request_control(
                    repo, supervisor, "cancel", expected_version=current["state_version"]
                )

    drain(queue, fault=fault)
    assert (
        cancelled and service.summary(repo, UUID(receipt.collaboration_id))["status"] == "cancelled"
    )
    current, _ = state(flow, "supervisor")
    assert (
        current.usage.model_calls == 2
        and current.usage.reserved_tokens == 0
        and current.pending is None
    )
    assert len(rows(queue, "external_calls")) == 20


class BoundaryReached(BaseException):
    """仅测试用的栈中断，不能被普通 Worker 异常结算吞掉。"""


def at(point):
    def stop(actual):
        if actual == point:
            raise BoundaryReached()

    return stop


def test_exact_sender_retry_does_not_repeat_dispatch_and_changed_batch_is_rejected(flow):
    queue, _, _, _, _, _ = flow
    start(flow)
    lease = queue.claim("sender-retry")
    runner = CollaborationRoleRunner(queue, lease, fault=at("collaboration.exchange_committed"))
    with pytest.raises(BoundaryReached):
        runner.run()
    exchange = runner.exchange
    address = exchange.address()
    old = parse_contract(
        encode(queue.history.restore(lease.repository_id, address.commit_id).data),
        CollaborationRoleState,
    )
    sends = tuple(
        (role, "dispatch", old.exports["message:" + task])
        for role, task in [
            (CollaborationRole.RESEARCHER, "collect"),
            (CollaborationRole.VERIFIER, "verify"),
            (CollaborationRole.WRITER, "write"),
        ]
    )
    exchange.history.fault = lambda _: None
    before = {
        n: len(rows(queue, n))
        for n in ["events", "commits", "collaboration_messages", "collaboration_receipts"]
    }
    current = exchange.load()
    assert (
        exchange.batch(
            old, sends=sends, publications=(), status=RunStatus.PAUSED, phase="await_result"
        )
        == current
    )
    for altered in [sends[:1], sends[::-1]]:
        with pytest.raises(AgentGitError) as caught:
            exchange.batch(
                old, sends=altered, publications=(), status=RunStatus.PAUSED, phase="await_result"
            )
        assert caught.value.code == ErrorCode.VERSION_CONFLICT
    with pytest.raises(AgentGitError) as caught:
        exchange.batch(old, sends=sends, publications=(), status=RunStatus.COMPLETED, phase="done")
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert {n: len(rows(queue, n)) for n in before} == before
    queue.finish(lease, "paused")
    drain(queue)
    assert len(rows(queue, "collaboration_messages")) == 7


def test_stale_shared_expected_version_rolls_back_entire_message_and_publication_batch(flow):
    queue, service, _, repo, receipt, _ = flow
    start(flow)
    Worker(queue).run_once()
    Worker(queue).run_once()
    assert [r["version"] for r in rows(queue, "collaboration_publications")] == [1]
    lease = queue.claim("shared-version")
    runner = CollaborationRoleRunner(queue, lease, fault=at("collaboration.export_ready_saved"))
    with pytest.raises(BoundaryReached):
        runner.run()
    exchange = runner.exchange
    current = exchange.load()
    assert current.role == CollaborationRole.VERIFIER
    sends = ((CollaborationRole.WRITER, "verification", current.exports["message"]),)
    before = {
        n: len(rows(queue, n))
        for n in ["events", "commits", "collaboration_messages", "collaboration_publications"]
    }
    with pytest.raises(AgentGitError) as caught:
        exchange.batch(
            current,
            sends=sends,
            publications=(
                ("evidence.json", 0, current.exports["evidence.json"]),
                ("verification.json", 0, current.exports["verification.json"]),
            ),
            status=RunStatus.COMPLETED,
            phase="done",
        )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert {n: len(rows(queue, n)) for n in before} == before
    exchange.history.fault = lambda _: None
    publications = (
        ("evidence.json", 1, current.exports["evidence.json"]),
        ("verification.json", 0, current.exports["verification.json"]),
    )
    saved = exchange.batch(
        current, sends=sends, publications=publications, status=RunStatus.COMPLETED, phase="done"
    )
    again = {n: len(rows(queue, n)) for n in before}
    assert (
        exchange.batch(
            current,
            sends=sends,
            publications=publications,
            status=RunStatus.COMPLETED,
            phase="done",
        )
        == saved
    )
    assert {n: len(rows(queue, n)) for n in before} == again
    queue.finish(lease, "completed")
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
    assert len(rows(queue, "collaboration_publications")) == 5


@pytest.mark.parametrize(
    "point",
    [
        "collaboration.call_reserved",
        "collaboration.after_call_intent",
        "collaboration.after_saved_result",
        "collaboration.export_ready_saved",
        "collaboration.exchange_projected",
        "collaboration.exchange_committed",
    ],
)
def test_actual_interpreter_death_recovers_atomic_exchange_or_keeps_unknown(
    flow, tmp_path, request, point
):
    queue, service, _, repo, receipt, roles = flow
    start(flow)
    with queue.history.engine.connect() as c:
        schema = c.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker, counts = tmp_path / "boundary.json", tmp_path / "actual-model-calls.jsonl"
    env = os.environ.copy()
    env.update(
        AGENTGIT_COLLAB_TEST_SCHEMA=schema,
        AGENTGIT_COLLAB_TEST_POINT=point,
        AGENTGIT_COLLAB_TEST_DATABASE=queue.history.engine.url.render_as_string(
            hide_password=False
        ),
        AGENTGIT_COLLAB_TEST_OBJECTS=str(queue.history.objects.root),
        AGENTGIT_COLLAB_TEST_DATA=str(queue.research.data_root),
        AGENTGIT_COLLAB_TEST_MARKER=str(marker),
        AGENTGIT_COLLAB_TEST_CALLS=str(counts),
        PYTHONIOENCODING="utf-8",
    )
    helper = Path(__file__).parents[1] / "helpers/collaboration_worker_process.py"
    process = subprocess.Popen(
        [sys.executable, str(helper)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = monotonic() + 30
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode("utf-8", errors="replace"))
            assert monotonic() < deadline, "实际角色 Worker 未抵达指定边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text("utf-8"))
        assert boundary["point"] == point and boundary["schema"] == schema
        assert boundary["pid"] == process.pid or (
            os.name == "nt" and boundary["parent_pid"] == process.pid
        )
        assert (
            boundary["source_revision"] == source_revision()
            and boundary["lock_digest"] == dependency_lock_digest()
        )
        assert process.poll() is None
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.kill()
        process.wait(timeout=10)
        assert process.returncode == (73 if os.name == "nt" else -9)
        before = (
            [json.loads(line) for line in counts.read_text("utf-8").splitlines()]
            if counts.exists()
            else []
        )
        assert len(before) == int(
            point not in ("collaboration.call_reserved", "collaboration.after_call_intent")
        )
        assert len(rows(queue, "collaboration_messages")) == (
            3 if point == "collaboration.exchange_committed" else 0
        )
        with queue.history.engine.begin() as c:
            job = TABLES["jobs"]
            target = UUID(roles["supervisor"]["job_id"])
            old_fence = c.execute(
                sa.select(job.c.fencing_token).where(job.c.id == target)
            ).scalar_one()
            c.execute(
                job.update()
                .where(job.c.id == target)
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        counted_model = runpy.run_path(str(helper))["counted_model"]

        recovered = Worker(
            queue, collaboration_model_factory=lambda role: counted_model(counts)
        ).run_once()
        passes = [
            recovered,
            *drain(queue, collaboration_model_factory=lambda role: counted_model(counts)),
        ]
        if point == "collaboration.after_call_intent":
            assert recovered["status"] == "outcome_unknown", recovered
        else:
            # 提交后的子任务与过期父租约都可领取；UUID 排序不保证父任务先接管。
            assert all(r["status"] in ("paused", "completed") for r in passes), passes
            assert any(r["run_id"] == roles["supervisor"]["run_id"] for r in passes)
        after = (
            [json.loads(line) for line in counts.read_text("utf-8").splitlines()]
            if counts.exists()
            else []
        )
        unknown = point == "collaboration.after_call_intent"
        assert len(after) == (0 if unknown else 5)
        assert len({v["request"] for v in after}) == len(after)
        assert len(rows(queue, "external_calls")) == (1 if unknown else 20)
        assert len(rows(queue, "collaboration_messages")) == (0 if unknown else 7)
        assert len(rows(queue, "collaboration_receipts")) == (0 if unknown else 7)
        assert len(rows(queue, "collaboration_publications")) == (0 if unknown else 5)
        assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == (
            "outcome_unknown" if unknown else "completed"
        )
        with queue.history.engine.connect() as c:
            new_fence = c.execute(
                sa.select(job.c.fencing_token).where(job.c.id == target)
            ).scalar_one()
        assert new_fence > old_fence
        request.node._collaboration_process = dict(
            boundary=boundary,
            launcher_pid=process.pid,
            exit_code=process.returncode,
            before_fence=old_fence,
            after_fence=new_fence,
            before_actual=before,
            after_actual=after,
            worker_passes=passes,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()


def test_private_payload_forged_parent_and_wrong_route_cannot_enter_outbox(flow, monkeypatch):
    queue, _, _, _, _, roles = flow
    start(flow)
    Worker(queue).run_once()
    lease = queue.claim("export-authority")
    runner = CollaborationRoleRunner(queue, lease, fault=at("collaboration.export_ready_saved"))
    with pytest.raises(BoundaryReached):
        runner.run()
    exchange = runner.exchange
    current = exchange.load()
    assert current.role == CollaborationRole.RESEARCHER
    before = {
        n: len(rows(queue, n))
        for n in ["events", "commits", "collaboration_messages", "collaboration_publications"]
    }
    for receiver, ref in [
        (CollaborationRole.VERIFIER, current.private_input_ref),
        (CollaborationRole.SUPERVISOR, current.exports["message"]),
    ]:
        with pytest.raises(AgentGitError) as caught:
            exchange.batch(
                current,
                sends=((receiver, "evidence", ref),),
                publications=(),
                status=RunStatus.COMPLETED,
                phase="done",
            )
        assert caught.value.code == ErrorCode.FORBIDDEN
    address = exchange.address()
    for forged in [
        evolve(address, seq=address.seq + 1),
        evolve(address, agent_run_id=roles["supervisor"]["run_id"]),
    ]:
        monkeypatch.setattr(exchange, "address", lambda value=forged: value)
        with pytest.raises(AgentGitError) as caught:
            exchange.batch(
                current,
                sends=((CollaborationRole.VERIFIER, "evidence", current.exports["message"]),),
                publications=(),
                status=RunStatus.COMPLETED,
                phase="done",
            )
        assert caught.value.code == ErrorCode.FORBIDDEN
    monkeypatch.setattr(exchange, "address", lambda: address)
    assert {n: len(rows(queue, n)) for n in before} == before
    runner.fault = lambda _: None
    assert runner.run()["status"] == "completed"
    drain(queue)


@pytest.mark.parametrize("flow", ["no-researcher-read", "no-writer-write"], indirect=True)
def test_removed_role_tool_permission_is_denied_without_borrowing_other_roles(flow):
    queue, service, _, repo, receipt, _ = flow
    start(flow)
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "failed"
    program = parse_contract(PROGRAM.read_bytes(), CollaborationProgram)
    calls = rows(queue, "external_calls")
    failures = []
    for role in ("researcher", "writer"):
        current, run = state(flow, role)
        own_calls = [c for c in calls if c["run_id"] == run["id"]]
        if current.error_code == "forbidden":
            failures.append(role)
            denied = "tool:source_read/v1" if role == "researcher" else "tool:artifact_write/v1"
            assert not any(c["provider"] == denied for c in own_calls)
            assert current.pending is None and current.usage.reserved_tokens == 0
        assert current.usage.model_calls <= program.policy(current.role).limits.max_model_calls
    assert len(failures) == 1
    assert not any(p["path"] == "report.md" for p in rows(queue, "collaboration_publications"))


@pytest.mark.parametrize("flow", ["writer-one-file"], indirect=True)
def test_writer_total_file_budget_stops_before_second_tool_write_and_result_publication(flow):
    queue, service, _, repo, receipt, _ = flow
    start(flow)
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "failed"
    current, run = state(flow, "writer")
    assert current.error_code == "output_limit" and current.pending is None
    writes = [
        c
        for c in rows(queue, "external_calls")
        if c["run_id"] == run["id"] and c["provider"] == "tool:artifact_write/v1"
    ]
    assert len(writes) == 1 and current.usage.tool_calls == 1
    assert len(queue.history.restore(repo, run["checkpoint_commit_id"]).artifacts) == 1
    assert not any(
        p["path"] in ("report.md", "sources.json")
        for p in rows(queue, "collaboration_publications")
    )
