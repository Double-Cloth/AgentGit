"""独立Recorded Run／Job的完整重算、零请求、控制及严格来源边界。"""

import copy
from uuid import UUID

import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from test_a2a_fixture import server as server
from test_a2a_recordings import source_case
from test_a2a_runs import ACTOR, case, control, ready_step

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import NewBranch
from agentgit.replay.a2a import A2ARecordedEngine
from agentgit.services.a2a_recordings import A2ARecordingReader
from agentgit.services.history import TABLES
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


def recorded_case(history, server, tmp_path, mode="success"):
    source, spec = source_case(history, server, tmp_path, mode)
    VersionService(history).fork(
        source[2],
        NewBranch(
            source_run_id=spec.source_run_id,
            from_commit=spec.from_commit,
            name="recorded",
            expected_version=0,
        ),
        actor=ACTOR,
    )
    receipt = source[1].submit(
        source[2],
        source[3],
        actor=ACTOR,
        key="recorded",
        branch_name="recorded",
        expected_head=spec.from_commit,
        expected_version=0,
        replay=spec,
    )
    return source, (*source[:4], receipt, *source[5:]), spec


def current(values):
    return values[1].recorded_state(values[2], UUID(values[4].run_id), actor=ACTOR)


def no_client(*args, **kwargs):
    pytest.fail("Recorded 不得创建或使用模型／协议／工具客户端")


def step(values, **kwargs):
    return ready_step(
        values,
        a2a_client_factory=no_client,
        mcp_client_factory=no_client,
        model_factory=no_client,
        **kwargs,
    )


def complete(values):
    for _ in range(140):
        result = step(values)
        if result["status"] != "queued":
            return result
    pytest.fail("Recorded 超过有界检查点数")


def source_fingerprint(history, source):
    run, job = UUID(source[4].run_id), UUID(source[4].job_id)
    with history.engine.connect() as c:
        return {
            name: [
                dict(r)
                for r in c.execute(
                    sa.select(TABLES[name]).where(
                        TABLES[name].c.id == identity
                        if name in {"runs", "jobs"}
                        else TABLES[name].c.run_id == run
                    )
                ).mappings()
            ]
            for name, identity in (
                ("runs", run),
                ("jobs", job),
                ("external_calls", run),
                ("events", run),
                ("snapshots", run),
                ("approvals", run),
                ("run_commits", run),
            )
        }


@pytest.mark.parametrize("mode", ["success", "structured", "poll", "input_required"])
def test_complete_recorded_run_recomputes_all_checkpoints_with_zero_actual_calls(
    history, server, tmp_path, monkeypatch, mode
):
    source, target, spec = recorded_case(history, server, tmp_path, mode)
    expected = A2ARecordedEngine(
        A2ARecordingReader(history).inspect(source[2], spec, actor=ACTOR)
    ).replay()
    before, requests = source_fingerprint(history, source), copy.deepcopy(source[6]())
    main = history.branch(source[2])
    monkeypatch.delenv(source[3].binding.server.credential_env)
    with monkeypatch.context() as patch:
        patch.setattr("agentgit.adapters.a2a.A2AClient.__init__", no_client)
        patch.setattr("http.client.HTTPConnection.connect", no_client)
        assert complete(target)["status"] == "completed"
    assert source[6]() == requests and source_fingerprint(history, source) == before
    assert history.branch(source[2]) == main
    state = current(target)
    assert (
        state.status.value == "completed"
        and state.actual_attempts == state.actual_response_bytes == 0
    )
    assert state.cursor == len(expected) - 1 and state.logical_state == expected[-1].state
    with history.engine.connect() as c:
        links, events, calls, approvals = (
            TABLES[n] for n in ("run_commits", "events", "external_calls", "approvals")
        )
        commits = list(
            c.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == UUID(target[4].run_id))
                .order_by(links.c.run_order)
            ).scalars()
        )
        assert (
            c.execute(sa.select(calls.c.id).where(calls.c.run_id == UUID(target[4].run_id))).first()
            is None
        )
        assert (
            c.execute(
                sa.select(approvals.c.id).where(approvals.c.run_id == UUID(target[4].run_id))
            ).first()
            is None
        )
        consumed = list(
            c.execute(
                sa.select(events).where(
                    events.c.run_id == UUID(target[4].run_id), events.c.type == "call.recorded"
                )
            ).mappings()
        )
    assert len(consumed) == len(expected[-1].state.observations)
    assert len(commits) == len(expected)
    for commit, frame in zip(commits, expected, strict=True):
        actual = history.restore(source[2], commit)
        logical = StateSnapshot(data=actual.data["logical_state"], artifacts=actual.artifacts)
        assert logical == frame.snapshot
        assert object_digest(
            ObjectType.STATE, encode(logical.model_dump(mode="json"))
        ) == object_digest(ObjectType.STATE, encode(frame.snapshot.model_dump(mode="json")))


def test_recorded_submit_original_key_and_program_are_exact(history, server, tmp_path):
    source, target, spec = recorded_case(history, server, tmp_path)
    assert (
        source[1].submit(
            source[2],
            source[3],
            actor=ACTOR,
            key="recorded",
            branch_name="recorded",
            expected_head=spec.from_commit,
            expected_version=0,
            replay=spec,
        )
        == target[4]
    )
    with pytest.raises(AgentGitError) as error:
        source[1].submit(
            source[2],
            source[3],
            actor=ACTOR,
            key="recorded",
            branch_name="different",
            expected_head=spec.from_commit,
            expected_version=0,
            replay=spec,
        )
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    changed = source[3].model_copy(update={"max_polls": 1})
    with pytest.raises(AgentGitError) as error:
        source[1].submit(
            source[2],
            changed,
            actor=ACTOR,
            key="changed",
            branch_name="recorded",
            expected_head=spec.from_commit,
            expected_version=0,
            replay=spec,
        )
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


@pytest.mark.parametrize("change", ["no_fork", "actor", "project"])
def test_recorded_submit_requires_explicit_owned_source_branch(history, server, tmp_path, change):
    source, spec = source_case(history, server, tmp_path)
    repo, actor = source[2], ACTOR
    if change == "actor":
        actor = "another-operator"
    if change == "project":
        repo = history.create_repository("其他项目")
    branch = history.branch(repo)
    with pytest.raises(AgentGitError) as error:
        source[1].submit(
            repo,
            source[3],
            actor=actor,
            key="forbidden",
            branch_name="main",
            expected_head=branch["head_commit_id"],
            expected_version=branch["version"],
            replay=spec,
        )
    assert error.value.code in {ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND}


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_queued_recorded_control_is_local_and_does_not_consume_history(
    history, server, tmp_path, action
):
    source, target, _ = recorded_case(history, server, tmp_path)
    requests = copy.deepcopy(source[6]())
    control(target, action)
    assert step(target)["status"] == ("paused" if action == "pause" else "cancelled")
    assert current(target).cursor == 0 and current(target).logical_state.usage.tool_calls == 0
    if action == "pause":
        control(target, "resume")
        assert complete(target)["status"] == "completed"
    assert source[6]() == requests


@pytest.mark.parametrize("cursor", [0, 1, 2, 3])
def test_pause_resume_preserves_exact_cursor_source_and_deadline(history, server, tmp_path, cursor):
    source, target, _ = recorded_case(history, server, tmp_path, "poll")
    for _ in range(cursor + 1):
        assert step(target)["status"] == "queued"
    logical = current(target).logical_state
    deadline = target[0].get(target[2], UUID(target[4].job_id))["deadline_at"]
    control(target, "pause")
    assert step(target)["status"] == "paused"
    assert current(target).cursor == cursor and current(target).logical_state == logical
    control(target, "resume")
    assert complete(target)["status"] == "completed"
    assert target[0].get(target[2], UUID(target[4].job_id))["deadline_at"] == deadline


def test_cancel_recorded_pending_history_does_not_send_remote_cancel(history, server, tmp_path):
    source, target, _ = recorded_case(history, server, tmp_path)
    assert step(target)["status"] == step(target)["status"] == "queued"
    old = current(target)
    requests = copy.deepcopy(source[6]())
    control(target, "cancel")
    assert step(target)["status"] == "cancelled"
    assert current(target).logical_state == old.logical_state and current(target).cursor == 1
    assert source[6]() == requests


def test_control_arriving_before_checkpoint_is_not_marked_applied_without_stopping(
    history, server, tmp_path
):
    _, target, _ = recorded_case(history, server, tmp_path)
    assert step(target)["status"] == "queued"
    once = False

    def fault(point):
        nonlocal once
        if point == "a2a.recorded.before_checkpoint" and not once:
            once = True
            control(target, "pause")

    assert step(target, fault=fault)["status"] == "queued"
    assert current(target).cursor == 0
    assert target[0].run(target[2], UUID(target[4].run_id))["control_status"] == "requested"
    assert step(target)["status"] == "paused"
    assert current(target).cursor == 0


def test_expired_original_deadline_is_not_reset_by_recorded_resume(history, server, tmp_path):
    _, target, _ = recorded_case(history, server, tmp_path)
    control(target, "pause")
    assert step(target)["status"] == "paused"
    jobs = TABLES["jobs"]
    with history.engine.begin() as c:
        c.execute(
            jobs.update()
            .where(jobs.c.id == UUID(target[4].job_id))
            .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    deadline = target[0].get(target[2], UUID(target[4].job_id))["deadline_at"]
    control(target, "resume")
    assert step(target)["status"] == "budget_exhausted"
    assert current(target).cursor == 0 and current(target).actual_attempts == 0
    assert target[0].get(target[2], UUID(target[4].job_id))["deadline_at"] == deadline


def test_missing_record_after_submission_fails_without_live_fallback(history, server, tmp_path):
    source, target, _ = recorded_case(history, server, tmp_path)
    assert step(target)["status"] == "queued"
    calls = TABLES["external_calls"]
    with history.engine.begin() as c:
        row = (
            c.execute(sa.select(calls).where(calls.c.run_id == UUID(source[4].run_id)))
            .mappings()
            .one()
        )
        c.execute(calls.update().where(calls.c.id == row["id"]).values(result_ref=None))
    requests = copy.deepcopy(source[6]())
    try:
        assert step(target)["status"] == "failed"
        assert current(target).status.value == "failed" and current(target).cursor == 0
        assert source[6]() == requests
    finally:
        with history.engine.begin() as c:
            c.execute(
                calls.update().where(calls.c.id == row["id"]).values(result_ref=row["result_ref"])
            )


@pytest.mark.parametrize("change", ["recorded_to_live", "live_to_recorded"])
def test_mode_payload_mismatch_never_falls_back_to_live(history, server, tmp_path, change):
    if change == "recorded_to_live":
        _, target, _ = recorded_case(history, server, tmp_path)
    else:
        target = case(history, server, tmp_path)
    with history.engine.begin() as c:
        c.execute(
            TABLES["runs"]
            .update()
            .where(TABLES["runs"].c.id == UUID(target[4].run_id))
            .values(mode="live" if change == "recorded_to_live" else "recorded")
        )
    before = copy.deepcopy(target[6]())
    assert step(target)["status"] == "failed"
    assert target[6]() == before
