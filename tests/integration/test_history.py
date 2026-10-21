"""S2：真实 PostgreSQL 事务、跨项目约束与恢复。"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService

pytestmark = pytest.mark.integration


def environment(history, repository):
    input_ref = history.register_input(repository, b"fixture input")
    return EnvironmentManifest(
        runtime_version="0.1.0",
        python_version="3.12",
        code_revision="s2-fixture",
        dependency_lock_digest="a" * 64,
        model_signature="mock/v1",
        prompt_digest="b" * 64,
        input_ref=input_ref,
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )


def commit(history, repository, run, *, value=1, **changes):
    branch = history.branch(repository)
    options = dict(
        expected_head=branch["head_commit_id"],
        expected_version=branch["version"],
        state={"value": value},
        environment=environment(history, repository),
        events=(EventInput("state.updated", "step", {"value": value}),),
        artifacts=(ArtifactInput("report.md", f"报告 {value}".encode(), "text/markdown"),),
    )
    return history.commit(repository, run, **{**options, **changes})


def test_restore_log_artifact_and_event_range(history):
    repository = history.create_repository("项目")
    run = history.create_run(repository)
    history.append_event(repository, run, EventInput("tool.started", "step", {"call": 0}))
    history.append_event(repository, run, EventInput("tool.completed", "step", {"call": 0}, 1))
    first = commit(history, repository, run)
    second = commit(history, repository, run, value=2)
    assert [item["seq"] for item in history.events(repository, run)] == [1, 2, 3, 4]
    assert [item["seq"] for item in history.events(repository, run, after=2, limit=1)] == [3]
    assert history.log(repository) == [second.commit_id, first.commit_id]
    restarted = HistoryService(history.engine, history.objects)
    assert restarted.restore(repository, first.commit_id).data == {"value": 1}
    assert restarted.restore(repository, second.commit_id).data == {"value": 2}
    artifact = history.artifacts(repository, first.commit_id)[0]
    assert history.read_artifact(repository, artifact["id"]) == "报告 1".encode()
    manifest = history.objects.get_json(first.commit_id, ObjectType.COMMIT)
    assert manifest["event_range"]["first_seq"] == 1
    assert manifest["event_range"]["last_seq"] == 3


def test_exactly_one_concurrent_head_update(history):
    repository = history.create_repository("竞争")
    run = history.create_run(repository)
    barrier = Barrier(2)

    def compete(index):
        service = HistoryService(
            history.engine,
            history.objects,
            fault=lambda point: barrier.wait(timeout=10) if point == "before_transaction" else None,
        )
        try:
            return commit(service, repository, run, value=index)
        except AgentGitError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(compete, range(2)))
    assert results.count(ErrorCode.VERSION_CONFLICT) == 1
    assert len(history.log(repository)) == 1
    with history.engine.connect() as connection:
        for name in ("commits", "snapshots", "run_commits", "events", "artifacts"):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                == 1
            )
    assert len(history.events(repository, run)) == 1


@pytest.mark.parametrize("point", ["before_transaction", "in_transaction", "after_commit"])
def test_transaction_fault_has_no_half_history(history, point):
    repository = history.create_repository("故障")
    run = history.create_run(repository)

    def fail(current):
        if current == point:
            raise RuntimeError("注入故障")

    service = HistoryService(history.engine, history.objects, fault=fail)
    with pytest.raises(RuntimeError):
        commit(service, repository, run)
    if point == "after_commit":
        head = history.branch(repository)["head_commit_id"]
        assert history.restore(repository, head).data == {"value": 1}
        assert len(history.events(repository, run)) == 1
    else:
        assert history.branch(repository)["head_commit_id"] is None
        assert history.events(repository, run) == []
        with history.engine.connect() as connection:
            for name in ("commits", "snapshots", "run_commits"):
                assert (
                    connection.execute(
                        sa.select(sa.func.count()).select_from(TABLES[name])
                    ).scalar_one()
                    == 0
                )
        assert history.gc_dry_run()["unindexed"]


def test_project_boundary_and_physical_dedup(history):
    first, other = history.create_repository("A"), history.create_repository("B")
    run_a, run_b = history.create_run(first), history.create_run(other)
    a, b = commit(history, first, run_a), commit(history, other, run_b)
    assert a.state_ref == b.state_ref
    assert a.commit_id != b.commit_id
    artifact = history.artifacts(first, a.commit_id)[0]
    for read in (
        lambda: history.restore(other, a.commit_id),
        lambda: history.read_artifact(other, artifact["id"]),
        lambda: history.events(other, run_a),
    ):
        with pytest.raises(AgentGitError) as caught:
            read()
        assert caught.value.code == ErrorCode.NOT_FOUND
    with history.engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            TABLES["branches"]
            .update()
            .where(TABLES["branches"].c.repository_id == other)
            .values(head_commit_id=a.commit_id)
        )
    with pytest.raises(AgentGitError):
        history.append_event(other, run_b, EventInput("bad", "step", {}, 1, run_a))


def test_missing_parent_and_illegal_parent_edges(history):
    repository = history.create_repository("父节点")
    run = history.create_run(repository)
    first = commit(history, repository, run)
    second = commit(history, repository, run, value=2)
    with pytest.raises(AgentGitError):
        commit(history, repository, run, expected_head="f" * 64)
    edges = TABLES["commit_parents"]
    for values in (
        dict(commit_id=first.commit_id, parent_id=first.commit_id, parent_order=0),
        dict(commit_id=second.commit_id, parent_id=first.commit_id, parent_order=0),
        dict(commit_id=first.commit_id, parent_id="f" * 64, parent_order=0),
        dict(commit_id=first.commit_id, parent_id=second.commit_id, parent_order=1),
    ):
        with history.engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(edges.insert().values(repository_id=repository, **values))
    assert history.log(repository) == [second.commit_id, first.commit_id]


def test_historical_rows_cannot_be_changed(history):
    repository = history.create_repository("不可变")
    run = history.create_run(repository)
    result = commit(history, repository, run)
    for name in (
        "commits",
        "objects",
        "events",
        "snapshots",
        "artifacts",
        "run_commits",
        "object_refs",
    ):
        with (
            history.engine.begin() as connection,
            pytest.raises(DBAPIError, match="immutable_history"),
        ):
            connection.execute(TABLES[name].delete())
    assert history.restore(repository, result.commit_id).data == {"value": 1}


def test_corrupt_or_missing_object_blocks_restore(history):
    repository = history.create_repository("损坏")
    run = history.create_run(repository)
    result = commit(history, repository, run)
    path = history.objects.path_for(result.state_ref)
    original = path.read_bytes()
    path.write_bytes(b"corrupt")
    with pytest.raises(AgentGitError) as caught:
        history.restore(repository, result.commit_id)
    assert caught.value.code == ErrorCode.OBJECT_CORRUPT
    path.write_bytes(original)
    assert history.restore(repository, result.commit_id).data == {"value": 1}
    path.unlink()
    with pytest.raises(AgentGitError) as caught:
        history.restore(repository, result.commit_id)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_stale_writer_guard_rolls_back(history):
    repository = history.create_repository("写者")
    run = history.create_run(repository)

    def guard(connection):
        raise AgentGitError(ErrorCode.VERSION_CONFLICT, "租约已过期")

    with pytest.raises(AgentGitError):
        commit(history, repository, run, writer_guard=guard)
    assert history.events(repository, run) == []
    assert history.branch(repository)["head_commit_id"] is None


@pytest.mark.parametrize("point", ["after_temp", "after_publish", "in_transaction", "after_commit"])
@pytest.mark.parametrize("with_history", [False, True])
def test_process_death_at_publication_and_transaction_points(history, point, with_history):
    repository = history.create_repository("进程崩溃")
    run = history.create_run(repository)
    previous = commit(history, repository, run, value=0) if with_history else None
    baseline = 1 if with_history else 0
    input_ref = environment(history, repository).input_ref
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    helper = Path(__file__).resolve().parents[1] / "helpers/history_crash_worker.py"
    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(history.objects.root),
            schema,
            str(repository),
            str(run),
            point,
            input_ref,
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    restarted = HistoryService(history.engine, history.objects)
    head = restarted.branch(repository)["head_commit_id"]
    if point == "after_commit":
        assert restarted.restore(repository, head).data == {"value": 1}
        assert len(restarted.events(repository, run)) == baseline + 1
        assert restarted.gc_dry_run()["unindexed"] == []
    else:
        assert head == (previous.commit_id if previous else None)
        assert len(restarted.events(repository, run)) == baseline
        with history.engine.connect() as connection:
            for name in ("commits", "snapshots", "run_commits", "artifacts"):
                assert (
                    connection.execute(
                        sa.select(sa.func.count()).select_from(TABLES[name])
                    ).scalar_one()
                    == baseline
                )
        report = restarted.gc_dry_run()
        assert report["temporary"] if point == "after_temp" else report["unindexed"]
    if previous:
        assert restarted.restore(repository, previous.commit_id).data == {"value": 0}
        old_artifact = restarted.artifacts(repository, previous.commit_id)[0]
        assert restarted.read_artifact(repository, old_artifact["id"]) == "报告 0".encode()


def test_parent_cannot_be_appended_after_publishing(history):
    repository = history.create_repository("父关系不可补写")
    run = history.create_run(repository)
    first = commit(history, repository, run)
    second = commit(history, repository, run, value=2)
    with (
        history.engine.begin() as connection,
        pytest.raises(IntegrityError, match="parent_history_already_published"),
    ):
        connection.execute(
            TABLES["commit_parents"]
            .insert()
            .values(
                repository_id=repository,
                commit_id=first.commit_id,
                parent_id=second.commit_id,
                parent_order=0,
            )
        )


def test_recovery_checks_event_and_input_dependencies(history):
    repository = history.create_repository("依赖完整性")
    run = history.create_run(repository)
    result = commit(history, repository, run)
    event_ref = history.events(repository, run)[0]["payload_ref"]
    history.objects.path_for(event_ref).unlink()
    with pytest.raises(AgentGitError) as caught:
        history.restore(repository, result.commit_id)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_cross_project_environment_input_rejected(history):
    repository, other = history.create_repository("输入 A"), history.create_repository("输入 B")
    run = history.create_run(other)
    foreign_ref = history.register_input(repository, b"private input only in A")
    foreign_environment = environment(history, repository).model_copy(
        update={"input_ref": foreign_ref}
    )
    with pytest.raises(AgentGitError) as caught:
        commit(history, other, run, environment=foreign_environment)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_unpublished_parent_cycle_rejected(history):
    repository = history.create_repository("父环")
    run = history.create_run(repository)
    result = commit(history, repository, run)
    with history.engine.begin() as connection:
        row = dict(
            connection.execute(
                sa.select(TABLES["commits"]).where(TABLES["commits"].c.id == result.commit_id)
            )
            .mappings()
            .one()
        )
        drafts = [
            history.objects.put_json(ObjectType.COMMIT, {"schema_version": 1, "draft": name})
            for name in ("A", "B")
        ]
        for draft in drafts:
            history._index(connection, draft)
            connection.execute(TABLES["commits"].insert().values(**{**row, "id": draft.id}))
        connection.execute(
            TABLES["commit_parents"]
            .insert()
            .values(
                repository_id=repository,
                commit_id=drafts[0].id,
                parent_id=drafts[1].id,
                parent_order=0,
            )
        )
    with history.engine.begin() as connection, pytest.raises(IntegrityError, match="parent_cycle"):
        connection.execute(
            TABLES["commit_parents"]
            .insert()
            .values(
                repository_id=repository,
                commit_id=drafts[1].id,
                parent_id=drafts[0].id,
                parent_order=0,
            )
        )
    assert history.log(repository) == [result.commit_id]


def test_gc_retains_durable_calls_reserved_references_and_history(history):
    repository = history.create_repository("GC")
    run = history.create_run(repository)
    result = commit(history, repository, run)
    intent = history.objects.put_json(ObjectType.CALL, {"schema_version": 1, "intent": "fake"})
    reserved = history.objects.put(ObjectType.BLOB, b"reserved approval fixture")
    orphan = history.objects.put(ObjectType.BLOB, b"published before database transaction")
    call_id = uuid4()
    with history.engine.begin() as connection:
        history._ref(connection, repository, intent, "external_call", str(call_id), "intent")
        history._ref(connection, repository, reserved, "approval_reserved", str(uuid4()), "payload")
        connection.execute(
            TABLES["external_calls"]
            .insert()
            .values(
                id=call_id,
                repository_id=repository,
                run_id=run,
                logical_step_id="pending",
                call_index=0,
                attempt=1,
                provider="fake/v1",
                signature_digest="a" * 64,
                request_digest="b" * 64,
                effect="NON_IDEMPOTENT_WRITE",
                intent_ref=intent.id,
                status="started",
            )
        )
    before = {str(path): path.read_bytes() for _, path in history.objects.inventory()}
    report = history.gc_dry_run()
    after = {str(path): path.read_bytes() for _, path in history.objects.inventory()}
    assert before == after
    assert {result.commit_id, result.state_ref, intent.id, reserved.id} <= set(report["retained"])
    assert orphan.id in report["unindexed"]
    assert history.restore(repository, result.commit_id).data == {"value": 1}


@pytest.mark.parametrize("value", [0.1, uuid4()])
def test_state_payload_cannot_silently_convert_non_json_values(history, value):
    repository = history.create_repository("严格内容")
    run = history.create_run(repository)
    with pytest.raises(AgentGitError) as caught:
        commit(history, repository, run, state={"value": value})
    assert caught.value.code == ErrorCode.VALIDATION
    with pytest.raises(AgentGitError):
        history.append_event(repository, run, EventInput("state.updated", "step", {"value": value}))
    assert history.events(repository, run) == []
    assert history.branch(repository)["head_commit_id"] is None
