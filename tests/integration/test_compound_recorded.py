"""复合 Recorded 必须追溯原录制重算，并证明每层来源检查点。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_recorded_replayer import replay_source as replay_source

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import BudgetLimits
from agentgit.core.versions import LiveReplay, RecordedRange, RecordedReplay
from agentgit.replay.engine import RecordedReplayer
from agentgit.runtime.engine import ResearchRuntime
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.recordings import RecordingReader
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def replay(manager, repository, source, first, last, name):
    response = manager.replay(
        repository,
        RecordedReplay(
            source_run_id=str(source), from_commit=first, until_commit=last, branch_name=name
        ),
        name,
    )
    return response


def points(history, job):
    with history.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(TABLES["replay_checkpoints"])
                .where(
                    TABLES["replay_checkpoints"].c.job_id == UUID(job),
                )
                .order_by(TABLES["replay_checkpoints"].c.source_order)
            ).mappings()
        )


@pytest.mark.parametrize("start,end", [(0, -1), (1, -1), (4, 9), (1, 1), (0, 1)])
def test_compound_recomputes_partial_zero_and_restoration_boundaries_offline(
    history, replay_source, monkeypatch, start, end
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    original = history.branch(session.repository_id)
    first = replay(
        manager, session.repository_id, session.run_id, commits[0], commits[-1], "level-one"
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, first["job_id"])
    path = [commits[0], *(row["commit_id"] for row in saved)]
    source_head = history.branch(session.repository_id, "level-one")
    nested = replay(
        manager, session.repository_id, first["run_id"], path[start], path[end], "level-two"
    )

    def forbidden(*args, **kwargs):
        pytest.fail("复合 Recorded 不能调用真实 Adapter、网络或创建宿主工作区")

    runtime_calls = []
    original_run = ResearchRuntime.run

    def run(runtime):
        runtime_calls.append(runtime)
        return original_run(runtime)

    with monkeypatch.context() as offline:
        offline.setattr(ResearchRuntime, "run", run)
        for entry in (
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.tools.ToolRegistry.invoke",
            "socket.create_connection",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
            "subprocess.Popen",
        ):
            offline.setattr(entry, forbidden)
        result = Worker(manager.jobs, model_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    assert bool(runtime_calls) == ((start, end) not in ((1, 1), (0, 1)))
    expected = path[start : (len(path) if end == -1 else end + 1)]
    proofs = points(history, nested["job_id"])
    assert [row["source_commit_id"] for row in proofs] == expected
    for row in proofs:
        assert history.restore(session.repository_id, row["commit_id"]) == history.restore(
            session.repository_id, row["source_commit_id"]
        )
    report = manager.replay_status(session.repository_id, UUID(nested["job_id"]))["replay"]
    assert report["verified"] and report["recomputed_checkpoints"] == len(expected) - 1
    assert report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
    assert history.branch(session.repository_id) == original
    assert history.branch(session.repository_id, "level-one") == source_head


def test_three_levels_preserve_duplicate_restores_and_do_not_copy_old_proofs(
    history, replay_source
):
    session, final, commits, _ = replay_source
    manager = ManagementService(session.service)
    source, first, last = session.run_id, commits[0], commits[-1]
    expected = len(commits)
    for level in range(3):
        response = replay(manager, session.repository_id, source, first, last, f"nested-{level}")
        result = Worker(manager.jobs).run_once()
        assert result["status"] == "completed", result
        saved = points(history, response["job_id"])
        assert len(saved) == expected
        assert history.restore(
            session.repository_id, saved[-1]["commit_id"]
        ).data == final.model_dump(mode="json")
        source, last = response["run_id"], saved[-1]["commit_id"]
        expected += 1


def expire(history, job):
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(job))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )


@pytest.mark.parametrize("recover_source", [False, True])
def test_compound_recovers_its_own_job_and_accepts_recovered_source_runs(
    history, replay_source, recover_source
):
    session, final, commits, _ = replay_source
    manager = ManagementService(session.service)
    source = replay(
        manager, session.repository_id, session.run_id, commits[8], commits[-1], "recover-source"
    )
    count = 0

    def crash(point):
        nonlocal count
        if point == "recorded_after_checkpoint":
            count += 1
            if count == 3:
                raise RuntimeError("复合回放证明提交后崩溃")

    if recover_source:
        with pytest.raises(RuntimeError, match="证明提交后崩溃"):
            Worker(manager.jobs, fault=crash).run_once()
        expire(history, source["job_id"])
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    source_job = manager.jobs.get(session.repository_id, UUID(source["job_id"]))
    source_run = manager.jobs.run(session.repository_id, source_job["run_id"])
    start = source_run["base_commit_id"]
    nested = replay(
        manager,
        session.repository_id,
        source_job["run_id"],
        start,
        source_job["checkpoint_commit_id"],
        "recover-nested",
    )
    count = 0
    with pytest.raises(RuntimeError, match="证明提交后崩溃"):
        Worker(manager.jobs, fault=crash).run_once()
    before = manager.jobs.get(session.repository_id, UUID(nested["job_id"]))
    expire(history, nested["job_id"])
    result = Worker(manager.jobs).run_once()
    assert result["status"] == "completed", result
    after = manager.jobs.get(session.repository_id, UUID(nested["job_id"]))
    assert after["run_id"] != before["run_id"] and after["deadline_at"] == before["deadline_at"]
    assert history.restore(
        session.repository_id, after["checkpoint_commit_id"]
    ).data == final.model_dump(mode="json")
    assert manager.replay_status(session.repository_id, UUID(nested["job_id"]))["replay"][
        "verified"
    ]


def test_compound_requires_original_response_after_submission_and_never_falls_back(
    history, replay_source, monkeypatch
):
    session, _, commits, load = replay_source
    manager = ManagementService(session.service)
    source = replay(
        manager, session.repository_id, session.run_id, commits[0], commits[-1], "missing-source"
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, source["job_id"])
    nested = replay(
        manager,
        session.repository_id,
        source["run_id"],
        saved[0]["commit_id"],
        saved[-1]["commit_id"],
        "missing-nested",
    )
    reference = load().recordings[0].saved.response_ref
    raw = history.objects.get(reference, ObjectType.BLOB)
    path = history.objects.path_for(reference)
    assert path.resolve().is_relative_to(history.objects.root.resolve())
    path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("缺少原始录制不能相信已有证明或回退真实调用")

    try:
        monkeypatch.setattr("agentgit.adapters.tools.ToolRegistry.invoke", forbidden)
        result = Worker(manager.jobs, model_factory=forbidden).run_once()
        assert result["status"] == "failed" and result["error_code"] == "recording_missing"
        report = manager.replay_status(session.repository_id, UUID(nested["job_id"]))["replay"]
        assert not report["verified"] and report["actual"]["model_calls"] == 0
    finally:
        history.objects.put(ObjectType.BLOB, raw)


@pytest.mark.parametrize("tamper", ["mapping", "snapshot", "event", "snapshot_bool", "event_bool"])
def test_compound_replayer_rejects_changed_projection_evidence(history, replay_source, tamper):
    from dataclasses import replace

    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    source = replay(
        manager, session.repository_id, session.run_id, commits[0], commits[-1], "tamper-source"
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, source["job_id"])
    bundle = RecordingReader(history).load(
        session.repository_id,
        RecordedRange(
            source_run_id=source["run_id"],
            from_commit=saved[0]["commit_id"],
            until_commit=saved[-1]["commit_id"],
        ),
        expected_environment=session.environment,
    )
    projection = bundle.projection
    assert projection is not None
    if tamper == "mapping":
        bundle = replace(
            bundle,
            projection=replace(
                projection,
                commits=(projection.commits[0], projection.commits[0], *projection.commits[2:]),
            ),
        )
    elif tamper in ("snapshot", "snapshot_bool"):
        commit, state = bundle.checkpoints[2]
        state = state.model_copy(
            update={
                "data": {**state.data, "plan_version": True if tamper == "snapshot_bool" else 999}
            }
        )
        bundle = replace(
            bundle, checkpoints=(*bundle.checkpoints[:2], (commit, state), *bundle.checkpoints[3:])
        )
    else:
        bundle = replace(
            bundle,
            projection=replace(
                projection,
                events=(
                    projection.events[0],
                    ("plan.accepted", {"plan_version": True})
                    if tamper == "event_bool"
                    else ("run.planned", {"invented": "evidence"}),
                    *projection.events[2:],
                ),
            ),
        )
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).run(bundle, session.config, session.service.corpus)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


@pytest.mark.parametrize("action", ["pause", "cancel", "deadline"])
def test_compound_control_keeps_source_state_and_persistent_deadline(
    history, replay_source, action
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    source = replay(
        manager, session.repository_id, session.run_id, commits[0], commits[-1], "control-source"
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, source["job_id"])
    source_head = history.branch(session.repository_id, "control-source")
    nested = replay(
        manager,
        session.repository_id,
        source["run_id"],
        saved[0]["commit_id"],
        saved[-1]["commit_id"],
        "control-nested",
    )
    job, run = UUID(nested["job_id"]), UUID(nested["run_id"])
    count = 0

    def control(point):
        nonlocal count
        if point != "recorded_after_checkpoint":
            return
        count += 1
        if count != 3:
            return
        if action == "deadline":
            with history.engine.begin() as connection:
                connection.execute(
                    TABLES["jobs"]
                    .update()
                    .where(TABLES["jobs"].c.id == job)
                    .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
                )
        else:
            row = manager.jobs.run(session.repository_id, run)
            manager.jobs.request_control(
                session.repository_id, run, action, expected_version=row["state_version"]
            )

    result = Worker(manager.jobs, fault=control).run_once()
    assert (
        result["status"]
        == {"pause": "paused", "cancel": "cancelled", "deadline": "budget_exhausted"}[action]
    )
    before = manager.jobs.get(session.repository_id, job)
    assert not manager.replay_status(session.repository_id, job)["replay"]["verified"]
    if action == "pause":
        row = manager.jobs.run(session.repository_id, run)
        manager.jobs.request_control(
            session.repository_id, run, "resume", expected_version=row["state_version"]
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        after = manager.jobs.get(session.repository_id, job)
        assert after["run_id"] == run and after["deadline_at"] == before["deadline_at"]
        assert manager.replay_status(session.repository_id, job)["replay"]["verified"]
    assert history.branch(session.repository_id, "control-source") == source_head


def test_compound_of_live_preserves_frozen_new_budget_baseline(history, replay_source):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    live = manager.replay(
        session.repository_id,
        LiveReplay(
            source_run_id=str(session.run_id),
            from_commit=commits[16],
            branch_name="baseline-live",
            budget=BudgetLimits(max_steps=2, max_model_calls=2),
        ),
        "baseline-live",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    chain = VersionService(history).log(session.repository_id, branch_name="baseline-live")["items"]
    first = next(
        item["commit_id"]
        for item in reversed(chain)
        if item["environment_ref"] == chain[0]["environment_ref"]
    )
    source = replay(
        manager,
        session.repository_id,
        live["run_id"],
        first,
        chain[0]["commit_id"],
        "baseline-recorded",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, source["job_id"])
    nested = replay(
        manager,
        session.repository_id,
        source["run_id"],
        saved[0]["commit_id"],
        saved[-1]["commit_id"],
        "baseline-compound",
    )
    result = Worker(manager.jobs).run_once()
    assert result["status"] == "completed", result
    current = manager.jobs.get(session.repository_id, UUID(nested["job_id"]))
    assert history.restore(
        session.repository_id, current["checkpoint_commit_id"]
    ) == history.restore(session.repository_id, chain[0]["commit_id"])


def test_compound_rejects_proof_transplanted_to_other_run_before_creating_branch(
    history, replay_source
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    source = replay(
        manager, session.repository_id, session.run_id, commits[0], commits[-1], "foreign-proof"
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, source["job_id"])
    with history.engine.begin() as connection:
        # 模拟数据库遭受损坏；业务接口本身不能关闭不可变保护。
        connection.execute(sa.text("ALTER TABLE replay_checkpoints DISABLE TRIGGER USER"))
        connection.execute(
            TABLES["replay_checkpoints"]
            .update()
            .where(
                TABLES["replay_checkpoints"].c.job_id == UUID(source["job_id"]),
                TABLES["replay_checkpoints"].c.source_order == 1,
            )
            .values(run_id=session.run_id)
        )
        connection.execute(sa.text("ALTER TABLE replay_checkpoints ENABLE TRIGGER USER"))
    with pytest.raises(AgentGitError) as error:
        replay(
            manager,
            session.repository_id,
            source["run_id"],
            saved[0]["commit_id"],
            saved[-1]["commit_id"],
            "must-not-exist",
        )
    assert error.value.code == ErrorCode.OBJECT_CORRUPT
    assert "must-not-exist" not in {
        row["name"] for row in VersionService(history).branches(session.repository_id)["items"]
    }


def test_compound_shares_memory_accounting_across_projection_and_original_recording(
    history, replay_source, monkeypatch
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    source = replay(
        manager, session.repository_id, session.run_id, commits[0], commits[-1], "memory-source"
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    saved = points(history, source["job_id"])
    root = RecordedRange(
        source_run_id=str(session.run_id), from_commit=commits[0], until_commit=commits[-1]
    )
    nested = RecordedRange(
        source_run_id=source["run_id"],
        from_commit=saved[0]["commit_id"],
        until_commit=saved[-1]["commit_id"],
    )
    reader = RecordingReader(history)

    def measured(request):
        sizes = []

        def account(payload):
            sizes.append(len(payload))
            return payload

        reader._read(session.repository_id, request, session.environment, (), account)
        return sum(sizes)

    root_bytes, total_bytes = measured(root), measured(nested)
    limit = max(root_bytes, total_bytes - root_bytes)
    assert limit < total_bytes
    monkeypatch.setattr("agentgit.services.recordings.MAX_RECORDED_BYTES", limit)
    reader.load(session.repository_id, root, expected_environment=session.environment)
    with pytest.raises(AgentGitError) as error:
        reader.load(session.repository_id, nested, expected_environment=session.environment)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


def test_compound_pure_engine_bounds_a_forged_deep_projection_chain(history, replay_source):
    from dataclasses import replace

    from agentgit.services.recordings import RecordedProjection

    session, _, _, load = replay_source
    bundle = load(0, 0)
    for _ in range(33):
        bundle = replace(
            bundle, projection=RecordedProjection(bundle, bundle.selection.commits, (None,))
        )
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).run(bundle, session.config, session.service.corpus)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT
