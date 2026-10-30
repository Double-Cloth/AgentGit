"""真实 Recorded 四任务准备、来源约束、取消与防止 Live 回退。"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, rows, start
from test_collaboration_execution import flow as flow
from test_collaboration_preparation import config_for, schema_signature
from test_collaboration_recordings import counts

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import ObjectType
from agentgit.core.collaboration import CollaborationProgram, CollaborationRole
from agentgit.core.collaboration_controls import CollaborationControlRequest
from agentgit.core.collaboration_identities import CollaborationIdentityMap
from agentgit.core.collaboration_runs import RolePrivateInput
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.runtime.collaboration_run import CollaborationRoleRunner
from agentgit.services import collaboration as module
from agentgit.services.collaboration import CollaborationService
from agentgit.services.collaboration_control import CollaborationCoordinator
from agentgit.services.collaboration_controls import CollaborationControlService
from agentgit.services.collaboration_recordings import CollaborationRecordingReader
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "collaboration-operator"


@pytest.fixture
def completed(history, tmp_path, monkeypatch):
    queue = JobService(ResearchService(history, tmp_path / "d"))
    service = CollaborationService(queue)
    repo = history.create_repository("Recorded 独立准备")
    program = parse_contract(
        (Path(__file__).parents[1] / "fixtures/collaboration/v1/program.json").read_bytes(),
        CollaborationProgram,
    )
    ids = iter(
        [
            UUID(int=10),
            *(UUID(int=n) for i in range(4) for n in (100 + i, 200 + i)),
            *(UUID(int=300 + i) for i in range(4)),
        ]
    )
    private = tuple(
        RolePrivateInput(
            role=r, notes=("private-" + r.value, str(UUID(int=100 + i)), str(UUID(int=10)))
        )
        for i, r in enumerate(CollaborationRole)
    )
    with monkeypatch.context() as selected:
        selected.setattr(module, "uuid4", lambda: next(ids))
        receipt = service.prepare(repo, program, actor=ACTOR, key="source", private_inputs=private)
    coordinator = CollaborationCoordinator(queue)
    roles = {r["role"]: r for r in service.summary(repo, UUID(receipt.collaboration_id))["roles"]}
    result = queue, service, coordinator, repo, receipt, roles
    start(result)
    drain(queue)
    return result


def prepare(completed, key="recorded"):
    queue, service, _, repo, source, _ = completed
    return service.prepare_recorded(repo, UUID(source.collaboration_id), actor=ACTOR, key=key)


def proof(request, **value):
    destination = os.environ.get("AGENTGIT_COLLABORATION_RECORDED_PREPARATION_PROOF_DIR")
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
        (root / name).write_text(
            json.dumps(dict(test=request.node.nodeid, **value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def test_recorded_preparation_creates_four_new_origins_and_preserves_literal_private_bytes(
    completed, monkeypatch, request
):
    queue, service, _, repo, source, source_roles = completed
    before = counts(queue)
    main = queue.history.branch(repo)

    def forbidden(*args, **kwargs):
        raise AssertionError("准备 Recorded 不能构造模型或工具")

    with monkeypatch.context() as selected:
        selected.setattr(MockCollaborationModel, "__init__", forbidden)
        selected.setattr(ToolRegistry, "__init__", forbidden)
        receipt = prepare(completed)
        after = counts(queue)
        assert prepare(completed) == receipt and counts(queue) == after
    summary = service.summary(repo, UUID(receipt.collaboration_id))
    link = rows(queue, "collaboration_recordings")[0]
    identities = parse_contract(
        queue.history.objects.get(link["identity_map_ref"], ObjectType.BLOB),
        CollaborationIdentityMap,
    )
    assert identities.source.collaboration_id == source.collaboration_id
    assert identities.target.collaboration_id == receipt.collaboration_id
    assert summary["mode"] == "recorded" and summary["status"] == "prepared"
    assert summary["source_collaboration_id"] == source.collaboration_id
    assert (
        after["runs"] - before["runs"]
        == after["jobs"] - before["jobs"]
        == after["collaboration_tasks"] - before["collaboration_tasks"]
        == 4
    )
    assert after["external_calls"] == before["external_calls"] == 20
    assert after["snapshots"] == before["snapshots"] and queue.claim("idle") is None
    tasks = rows(queue, "collaboration_tasks")
    for target in summary["roles"]:
        source_role = source_roles[target["role"]]
        run = queue.run(repo, UUID(target["run_id"]))
        job = queue.get(repo, UUID(target["job_id"]))
        assert run["mode"] == "recorded" and str(run["origin_run_id"]) == source_role["run_id"]
        assert job["origin_run_id"] == run["origin_run_id"] and run["base_commit_id"] is None
        assert run["checkpoint_commit_id"] is None and run["status"] == job["status"] == "paused"
        old = next(t for t in tasks if str(t["run_id"]) == source_role["run_id"])
        new = next(t for t in tasks if str(t["run_id"]) == target["run_id"])
        for field in ("private_input_ref", "policy_ref", "budget_ref"):
            assert old[field] == new[field]
        notes = queue.history.objects.get_json(new["private_input_ref"], ObjectType.BLOB)["notes"]
        assert notes[1] == source_role["run_id"] and notes[1] != target["run_id"]
    assert queue.history.branch(repo) == main
    proof(
        request,
        source_group=source.collaboration_id,
        target_group=receipt.collaboration_id,
        before=before,
        after=after,
        target_status="prepared",
        literal_private_preserved=True,
    )


@pytest.mark.parametrize("point", ["collaboration.role_prepared", "collaboration.before_commit"])
def test_recorded_preparation_rolls_back_all_four_roles(completed, point, request):
    queue = completed[0]
    before = counts(queue)

    def fail(current):
        if current == point:
            raise RuntimeError("Recorded 原子准备故障")

    queue.history.fault = fail
    with pytest.raises(RuntimeError, match="原子准备故障"):
        prepare(completed)
    assert counts(queue) == before
    proof(request, fault=point, before=before, after=counts(queue))


def test_concurrent_recorded_original_key_creates_one_group(completed, request):
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: prepare(completed), range(2)))
    assert receipts[0] == receipts[1] and counts(completed[0])["collaboration_recordings"] == 1
    assert counts(completed[0])["runs"] == 8
    proof(request, target_group=receipts[0].collaboration_id, concurrent_receipts_equal=True)


@pytest.mark.parametrize("scope", ["actor", "repository"])
def test_recorded_source_authorization_precedes_private_reads(completed, scope, monkeypatch):
    queue, service, _, repo, source, _ = completed

    def forbidden(*args):
        raise AssertionError("身份拒绝前不能读私有 CAS")

    with monkeypatch.context() as selected:
        selected.setattr(queue.history.objects, "get", forbidden)
        with pytest.raises(AgentGitError) as error:
            service.prepare_recorded(
                uuid4() if scope == "repository" else repo,
                UUID(source.collaboration_id),
                actor="other" if scope == "actor" else ACTOR,
                key="denied",
            )
        assert error.value.code == ErrorCode.FORBIDDEN
    assert counts(queue)["collaboration_recordings"] == 0


def test_missing_source_bytes_create_no_recorded_group(completed):
    queue = completed[0]
    before = counts(queue)
    call = rows(queue, "external_calls")[0]
    ref = queue.history.objects.get_json(call["result_ref"], ObjectType.CALL)["response_ref"]
    path = queue.history.objects.path_for(ref)
    raw = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(AgentGitError) as error:
            prepare(completed)
        assert error.value.code == ErrorCode.NOT_FOUND and counts(queue) == before
    finally:
        path.write_bytes(raw)


def test_prepared_recorded_cannot_start_or_construct_live_runner_but_lease_reads_only_own_input(
    completed, request
):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    summary = service.summary(repo, UUID(receipt.collaboration_id))
    with pytest.raises(AgentGitError) as error:
        coordinator.start(repo, UUID(receipt.collaboration_id), actor=ACTOR, key="start")
    assert error.value.code == ErrorCode.UNSUPPORTED_SCHEMA
    role = next(r for r in summary["roles"] if r["role"] == "supervisor")
    with queue.history.engine.begin() as c:
        c.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(role["job_id"]))
            .values(status="queued")
        )
    lease = queue.claim("scope-check")
    assert str(lease.run_id) == role["run_id"]
    scope = service.role_input(lease)
    assert scope.private_input.notes[1] == completed[5]["supervisor"]["run_id"]
    other = next(r for r in summary["roles"] if r["role"] == "writer")
    with pytest.raises(AgentGitError):
        service.role_input(lease, run_id=UUID(other["run_id"]))
    with pytest.raises(AgentGitError):
        service.role_input(replace(lease, fencing_token=lease.fencing_token + 1))

    def forbidden(role):
        raise AssertionError("Recorded 不能构造 Live 模型")

    with pytest.raises(AgentGitError) as error:
        CollaborationRoleRunner(queue, lease, fault=lambda _: None, model_factory=forbidden)
    assert error.value.code == ErrorCode.FORBIDDEN
    proof(
        request,
        target_group=receipt.collaboration_id,
        forced_lease_for_scope_test=True,
        live_factory_constructed=False,
    )


def cancel(queue, repo, group):
    return CollaborationControlService(queue).request(
        repo,
        UUID(group),
        CollaborationControlRequest(action="cancel", expected_revision=0),
        actor=ACTOR,
        key="cancel",
    )


def test_recorded_prepared_group_can_cancel_without_any_model_or_tool(
    completed, monkeypatch, request
):
    queue, service, _, repo, _, _ = completed
    receipt = prepare(completed)
    before = len(rows(queue, "external_calls"))
    cancel(queue, repo, receipt.collaboration_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("取消不能构造模型或工具")

    with monkeypatch.context() as selected:
        selected.setattr(MockCollaborationModel, "__init__", forbidden)
        selected.setattr(ToolRegistry, "__init__", forbidden)
        assert [r["status"] for r in drain(queue)] == ["cancelled"] * 4
    members = service.summary(repo, UUID(receipt.collaboration_id))["roles"]
    assert all(queue.run(repo, UUID(r["run_id"]))["status"] == "cancelled" for r in members)
    assert len(rows(queue, "external_calls")) == before == 20
    proof(
        request, target_group=receipt.collaboration_id, actual_recorded_calls=0, cancelled_roles=4
    )


def test_cancel_checkpoint_can_be_reclaimed_before_job_finish(completed, monkeypatch, request):
    queue, _, _, repo, _, _ = completed
    receipt = prepare(completed)
    cancel(queue, repo, receipt.collaboration_id)

    class Stopped(BaseException):
        pass

    original = queue.finish
    stopped = []

    def finish(lease, status):
        if not stopped:
            stopped.append(lease)
            raise Stopped()
        return original(lease, status)

    monkeypatch.setattr(queue, "finish", finish)
    with pytest.raises(Stopped):
        Worker(queue).run_once()
    saved = queue.run(repo, stopped[0].run_id)["checkpoint_commit_id"]
    with queue.history.engine.begin() as c:
        c.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == stopped[0].job_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    assert Worker(queue).run_once()["status"] == "cancelled"
    assert queue.run(repo, stopped[0].run_id)["checkpoint_commit_id"] == saved
    assert queue.get(repo, stopped[0].job_id)["fencing_token"] > stopped[0].fencing_token
    drain(queue)
    proof(
        request,
        target_group=receipt.collaboration_id,
        simulated_exit_after_checkpoint=True,
        unchanged_checkpoint=saved,
    )


def test_recording_link_is_immutable_and_blocks_downgrade(completed):
    queue = completed[0]
    prepare(completed)
    for action in (
        TABLES["collaboration_recordings"].delete(),
        TABLES["collaboration_recordings"].update().values(source_index_ref="0" * 64),
    ):
        with pytest.raises(DBAPIError), queue.history.engine.begin() as c:
            c.execute(action)
    with queue.history.engine.begin() as c:
        with pytest.raises(RuntimeError, match="已有协作 Recorded"):
            command.downgrade(config_for(c), "0030_collaboration_exchange")
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config_for(c, testing=False), "0030_collaboration_exchange")


def test_database_rejects_wrong_role_origin_even_if_factory_is_called_with_forged_index(
    completed, request
):
    queue, service, _, repo, source, _ = completed
    prepared = CollaborationRecordingReader(queue.history).inspect(
        repo, UUID(source.collaboration_id), actor=ACTOR
    )
    lanes = list(prepared.index.roles)
    a, b = lanes[0].run_id, lanes[1].run_id
    lanes[0] = lanes[0].model_copy(update=dict(run_id=b))
    lanes[1] = lanes[1].model_copy(update=dict(run_id=a))
    prepared = replace(prepared, index=prepared.index.model_copy(update=dict(roles=tuple(lanes))))
    raw = {ref: data for ref, _, data in prepared.objects}
    before = counts(queue)
    with pytest.raises(DBAPIError):
        service._prepare(
            repo,
            parse_contract(raw[prepared.index.program_ref], CollaborationProgram),
            actor=ACTOR,
            key="forged",
            private_inputs=tuple(
                parse_contract(raw[r.private_input_ref], RolePrivateInput)
                for r in prepared.index.roles
            ),
            _source=prepared,
        )
    assert counts(queue) == before
    proof(request, test_only_forged_role_origin=True, before=before, after=counts(queue))


@pytest.mark.parametrize("role", ["source_index", "identities"])
def test_database_requires_exact_recording_object_ownership(completed, role, monkeypatch):
    queue = completed[0]
    before = counts(queue)
    original = queue.history._ref

    def reference(c, repo, obj, owner, identity, name):
        if owner == "collaboration_recording" and name == role:
            return
        return original(c, repo, obj, owner, identity, name)

    monkeypatch.setattr(queue.history, "_ref", reference)
    with pytest.raises(DBAPIError):
        prepare(completed)
    assert counts(queue) == before


def test_recorded_preparation_requires_successful_source(flow):
    queue, service, _, repo, source, _ = flow
    before = counts(queue)
    with pytest.raises(AgentGitError) as error:
        service.prepare_recorded(repo, UUID(source.collaboration_id), actor=ACTOR, key="incomplete")
    assert error.value.code == ErrorCode.REPLAY_MISMATCH and counts(queue) == before


def test_migration_roundtrip_adds_one_table_and_restores_previous_shape(history):
    with history.engine.begin() as c:
        config = config_for(c)
        full = schema_signature(c)
        command.downgrade(config, "0030_collaboration_exchange")
        assert set(full) - set(schema_signature(c)) == {"collaboration_recordings"}
        command.upgrade(config, "head")
        assert schema_signature(c) == full
