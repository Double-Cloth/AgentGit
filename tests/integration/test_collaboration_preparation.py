"""真实隔离 Schema 的四任务原子准备与租约私有输入；尚不执行角色。"""

import base64
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
from test_kernel_migrations import config_for, schema_signature

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.collaboration import CollaborationProgram, CollaborationRole
from agentgit.core.collaboration_runs import RolePrivateInput
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.services.collaboration import CollaborationService
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.management import public
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration
PROGRAM = Path(__file__).parents[1] / "fixtures/collaboration/v1/program.json"
NAMES = (
    "collaborations",
    "collaboration_tasks",
    "runs",
    "jobs",
    "branches",
    "idempotency_keys",
    "objects",
    "object_refs",
    "tasks",
    "agent_configs",
    "external_calls",
)


@pytest.fixture(autouse=True)
def archive(history, request):
    yield
    destination = os.environ.get("AGENTGIT_COLLABORATION_EVIDENCE_DIR")
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    with history.engine.connect() as connection:
        tables = {
            name: [dict(row) for row in connection.execute(sa.select(TABLES[name])).mappings()]
            for name in NAMES
        }
        head = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    objects = []
    for row in tables["objects"]:
        raw = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            dict(
                id=row["id"], type=row["object_type"], base64=base64.b64encode(raw).decode("ascii")
            )
        )
    packet = dict(
        kind="collaboration-preparation-raw/v1",
        test=request.node.nodeid,
        source_revision=source_revision(),
        lock_digest=dependency_lock_digest(),
        schema_head=head,
        tables=public(tables),
        objects=objects,
        role_execution=False,
    )
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
    with (root / name).open("x", encoding="utf-8") as stream:
        json.dump(packet, stream, ensure_ascii=False, indent=2)


@pytest.fixture
def case(history, tmp_path):
    program = parse_contract(PROGRAM.read_bytes(), CollaborationProgram)
    queue = JobService(ResearchService(history, tmp_path / "roles"))
    service = CollaborationService(queue)
    repo = history.create_repository("四角色准备边界")
    private = tuple(
        RolePrivateInput(role=role, notes=("private-" + role.value,)) for role in CollaborationRole
    )
    return service, queue, repo, program, private


def prepare(case, key="prepare"):
    service, _, repo, program, private = case
    receipt = service.prepare(
        repo, program, actor="collaboration-operator", key=key, private_inputs=private
    )
    return receipt, service.summary(repo, UUID(receipt.collaboration_id))


def counts(history):
    with history.engine.connect() as connection:
        return {
            n: connection.execute(sa.select(sa.func.count()).select_from(TABLES[n])).scalar_one()
            for n in NAMES
        }


def rejected(code, callback):
    with pytest.raises(AgentGitError) as caught:
        callback()
    assert caught.value.code == code


def lease_for(case, role):
    service, queue, repo, _, _ = case
    receipt, summary = prepare(case)
    target = next(row for row in summary["roles"] if row["role"] == role)
    # 仅测试准备输入读取：显式激活自有单个 Job，然后使用真实队列领取；不调用 Worker。
    with queue.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(target["job_id"]))
            .values(status="queued")
        )
    lease = queue.claim("collaboration-input-fixture")
    assert (
        lease is not None and lease.run_id == UUID(target["run_id"]) and lease.repository_id == repo
    )
    return receipt, summary, target, lease


def test_preparation_is_four_real_independent_tasks_and_original_main_unchanged(case):
    service, queue, repo, program, _ = case
    main = queue.history.branch(repo)
    receipt, summary = prepare(case)
    assert summary["status"] == "prepared" and len(summary["roles"]) == 4
    assert (
        len({r["run_id"] for r in summary["roles"]})
        == len({r["task_id"] for r in summary["roles"]})
        == 4
    )
    assert queue.claim("ordinary-worker") is None
    with queue.history.engine.connect() as connection:
        roles = connection.execute(sa.select(TABLES["collaboration_tasks"])).mappings().all()
        for role in roles:
            budget = parse_contract(
                queue.history.objects.get(role["budget_ref"], ObjectType.BLOB),
                type(program.roles[0].limits),
            )
            assert budget == program.policy(CollaborationRole(role["role"])).limits
            job = queue.get(repo, role["job_id"])
            run = queue.run(repo, role["run_id"])
            assert (
                job["status"] == run["status"] == "paused" and job["kind"] == "collaboration_role"
            )
            assert (
                run["task_id"] is None
                and run["agent_config_id"] is None
                and run["origin_run_id"] is None
            )
    assert queue.history.branch(repo) == main
    assert counts(queue.history)["tasks"] == counts(queue.history)["agent_configs"] == 0
    assert "private-" not in json.dumps(summary) and "private_input_ref" not in json.dumps(summary)
    assert "notes" not in encode(receipt.model_dump(mode="json")).decode()
    assert (
        service.prepare(
            repo,
            program,
            actor="collaboration-operator",
            key="prepare",
            private_inputs=tuple(reversed(case[4])),
        )
        == receipt
    )


@pytest.mark.parametrize("role", list(CollaborationRole))
def test_own_lease_reads_only_own_private_context_after_reopening(case, role, tmp_path):
    service, queue, _, _, _ = case
    _, summary, target, lease = lease_for(case, role)
    reopened = CollaborationService(
        JobService(
            ResearchService(
                HistoryService(queue.history.engine, queue.history.objects), tmp_path / "reopened"
            )
        )
    )
    actual = reopened.role_input(lease)
    assert actual.private_input.notes == ("private-" + role.value,)
    assert actual.private_input.role == actual.policy.role == role
    assert not hasattr(actual, "corpus")
    for other in summary["roles"]:
        if other["run_id"] != target["run_id"]:
            rejected(
                ErrorCode.FORBIDDEN,
                lambda other=other: service.role_input(lease, run_id=UUID(other["run_id"])),
            )


@pytest.mark.parametrize(
    "field", ["run", "job", "repository", "worker", "fencing", "expired", "paused"]
)
def test_private_input_needs_current_exact_lease(case, field):
    service, queue, _, _, _ = case
    _, summary, _, lease = lease_for(case, CollaborationRole.RESEARCHER)
    other = next(r for r in summary["roles"] if r["run_id"] != str(lease.run_id))
    bad = lease
    if field == "run":
        bad = replace(lease, run_id=UUID(other["run_id"]))
    elif field == "job":
        bad = replace(lease, job_id=UUID(other["job_id"]))
    elif field == "repository":
        bad = replace(lease, repository_id=uuid4())
    elif field == "worker":
        bad = replace(lease, worker="not-the-lease-worker")
    elif field == "fencing":
        bad = replace(lease, fencing_token=lease.fencing_token + 1)
    else:
        with queue.history.engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == lease.job_id)
                .values(
                    **(
                        {"lease_until": sa.func.clock_timestamp() - sa.text("interval '1 second'")}
                        if field == "expired"
                        else {"status": "paused"}
                    )
                )
            )
    rejected(ErrorCode.VERSION_CONFLICT, lambda: service.role_input(bad))


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_normal_control_cannot_start_or_requeue_prepared_role(case, action):
    _, queue, repo, _, _ = case
    receipt, _ = prepare(case)
    before = counts(queue.history)
    expected = ErrorCode.VERSION_CONFLICT if action == "pause" else ErrorCode.FORBIDDEN
    rejected(
        expected,
        lambda: queue.request_control(
            repo, UUID(receipt.supervisor_run_id), action, expected_version=0
        ),
    )
    assert counts(queue.history) == before and queue.claim("ordinary-worker") is None


def test_private_input_does_not_trust_cross_project_or_changed_environment(case, monkeypatch):
    service, queue, repo, _, _ = case
    receipt, _, _, lease = lease_for(case, CollaborationRole.WRITER)
    alien = queue.history.create_repository("另一个项目")
    rejected(ErrorCode.NOT_FOUND, lambda: service.summary(alien, UUID(receipt.collaboration_id)))
    rejected(
        ErrorCode.VERSION_CONFLICT, lambda: service.role_input(replace(lease, repository_id=alien))
    )
    monkeypatch.setattr("agentgit.services.collaboration.source_revision", lambda: "changed-source")
    rejected(ErrorCode.REPLAY_MISMATCH, lambda: service.role_input(lease))
    assert queue.run(repo, lease.run_id)["status"] == "paused"


def test_same_key_concurrent_creators_publish_one_complete_group(case):
    service, queue, repo, program, private = case

    def submit():
        return service.prepare(
            repo, program, actor="concurrent", key="same", private_inputs=private
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: submit(), range(2)))
    assert a == b
    actual = counts(queue.history)
    assert (
        actual["collaborations"] == 1
        and actual["collaboration_tasks"] == actual["runs"] == actual["jobs"] == 4
    )
    altered = (RolePrivateInput(role=private[0].role, notes=("changed",)), *private[1:])
    rejected(
        ErrorCode.VERSION_CONFLICT,
        lambda: service.prepare(
            repo, program, actor="concurrent", key="same", private_inputs=altered
        ),
    )
    assert counts(queue.history) == actual


@pytest.mark.parametrize("boundary", [1, 2, 3, 4, 5])
def test_partial_four_role_preparation_rolls_back_and_original_key_recovers(case, boundary):
    service, queue, _, _, _ = case
    before = counts(queue.history)
    seen = 0

    def fault(point):
        nonlocal seen
        if point == "collaboration.role_prepared":
            seen += 1
        if (point == "collaboration.role_prepared" and seen == boundary) or (
            point == "collaboration.before_commit" and boundary == 5
        ):
            raise RuntimeError("测试原子准备中断")

    queue.history.fault = fault
    with pytest.raises(RuntimeError, match="原子准备中断"):
        prepare(case)
    assert counts(queue.history) == before
    queue.history.fault = lambda _: None
    receipt, _ = prepare(case)
    assert receipt == prepare(case)[0] and counts(queue.history)["collaboration_tasks"] == 4


def test_private_context_limits_and_repeated_role_fail_before_publication(case):
    service, queue, repo, program, private = case
    before = counts(queue.history)
    rejected(
        ErrorCode.VALIDATION,
        lambda: service.prepare(
            repo, program, actor="a", key="duplicate", private_inputs=(private[0], private[0])
        ),
    )
    value = program.model_dump(mode="json")
    value["roles"][1]["limits"]["max_context_bytes"] = 128
    limited = parse_contract(encode(value), CollaborationProgram)
    rejected(
        ErrorCode.CONTEXT_LIMIT,
        lambda: service.prepare(
            repo,
            limited,
            actor="a",
            key="limit",
            private_inputs=(
                RolePrivateInput(role=CollaborationRole.RESEARCHER, notes=("界" * 100,)),
            ),
        ),
    )
    rejected(
        ErrorCode.NOT_FOUND, lambda: service.prepare(uuid4(), program, actor="a", key="missing")
    )
    assert counts(queue.history) == before


def test_database_rejects_mutation_and_commit_of_incomplete_group(case):
    _, queue, repo, _, _ = case
    receipt, _ = prepare(case)
    for name, field in [
        ("collaboration_tasks", "private_input_ref"),
        ("collaborations", "bindings_ref"),
    ]:
        with pytest.raises(DBAPIError), queue.history.engine.begin() as connection:
            connection.execute(TABLES[name].update().values(**{field: "f" * 64}))
    groups = TABLES["collaborations"]
    with pytest.raises(DBAPIError), queue.history.engine.begin() as connection:
        row = (
            connection.execute(
                sa.select(groups).where(groups.c.id == UUID(receipt.collaboration_id))
            )
            .mappings()
            .one()
        )
        new = uuid4()
        for role, field, kind in [
            ("program", "program_ref", ObjectType.BLOB),
            ("bindings", "bindings_ref", ObjectType.BLOB),
            ("environment", "environment_ref", ObjectType.ENVIRONMENT),
        ]:
            obj = queue.history.objects.put(kind, queue.history.objects.get(row[field], kind))
            queue.history._ref(connection, repo, obj, "collaboration", str(new), role)
        connection.execute(
            groups.insert().values(
                id=new,
                repository_id=repo,
                program_ref=row["program_ref"],
                bindings_ref=row["bindings_ref"],
                environment_ref=row["environment_ref"],
                actor="incomplete",
            )
        )
    assert counts(queue.history)["collaborations"] == 1


def test_migration_roundtrip_adds_only_two_domain_tables(history):
    with history.engine.begin() as connection:
        before = schema_signature(connection)
        config = config_for(connection)
        command.downgrade(config, "0028_langgraph_runs")
        assert set(before) - set(sa.inspect(connection).get_table_names()) == {
            "collaborations",
            "collaboration_tasks",
        }
        command.upgrade(config, "head")
        assert schema_signature(connection) == before


def test_existing_group_blocks_destructive_or_non_testing_downgrade(case):
    _, queue, _, _, _ = case
    prepare(case)
    with queue.history.engine.begin() as connection:
        with pytest.raises(RuntimeError, match="已有协作数据"):
            command.downgrade(config_for(connection), "0028_langgraph_runs")
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config_for(connection, testing=False), "0028_langgraph_runs")
    assert counts(queue.history)["collaboration_tasks"] == 4
