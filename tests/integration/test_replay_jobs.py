"""Recorded 的独立耐久任务、原子证明与无真实 Adapter 的 Worker 路径。"""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_recorded_replayer import replay_source as replay_source
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.versions import RecordedReplay
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def submit(replay_source, *, until=-1, branch="recorded-job", key="recorded-submit"):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    request = RecordedReplay(
        source_run_id=str(session.run_id),
        from_commit=commits[0],
        until_commit=commits[until],
        branch_name=branch,
    )
    response = manager.replay(session.repository_id, request, key)
    return manager, request, response


def test_recorded_job_enqueues_without_execution_then_worker_proves_all_hashes(
    history, replay_source, monkeypatch
):
    session, final, commits, _ = replay_source
    original = history.branch(session.repository_id)
    manager, request, response = submit(replay_source)
    job_id, run_id = UUID(response["job_id"]), UUID(response["run_id"])
    assert manager.replay(session.repository_id, request, "recorded-submit") == response
    assert manager.jobs.get(session.repository_id, job_id)["status"] == "queued"
    assert not (session.service.data_root / "workspaces" / str(run_id)).exists()
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["replay_checkpoints"])
            ).scalar_one()
            == 0
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Recorded Worker 不能构造模型、工作区或执行服务")

    with monkeypatch.context() as offline:
        for entry in (
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
            "agentgit.adapters.execution.container.SandboxContainer.__init__",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.tools.ToolRegistry.invoke",
            "subprocess.Popen",
            "socket.create_connection",
        ):
            offline.setattr(entry, forbidden)
        result = Worker(manager.jobs, model_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    current = manager.jobs.run(session.repository_id, run_id)
    snapshot = history.restore(session.repository_id, current["checkpoint_commit_id"])
    assert snapshot.data == final.model_dump(mode="json")
    assert history.branch(session.repository_id) == original
    with history.engine.connect() as connection:
        rows = list(
            connection.execute(
                sa.select(TABLES["replay_checkpoints"])
                .where(TABLES["replay_checkpoints"].c.job_id == job_id)
                .order_by(TABLES["replay_checkpoints"].c.source_order)
            ).mappings()
        )
        assert [row["source_commit_id"] for row in rows] == list(commits)
        assert all(
            row["actual_model_calls"]
            == row["actual_tool_calls"]
            == row["actual_cost_micro_units"]
            == 0
            for row in rows
        )
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["external_calls"])
                .where(TABLES["external_calls"].c.run_id == run_id)
            ).scalar_one()
            == 0
        )
    for row in rows:
        assert history.restore(session.repository_id, row["commit_id"]) == history.restore(
            session.repository_id, row["source_commit_id"]
        )
    report = manager.replay_status(session.repository_id, job_id)["replay"]
    assert report["verified"] and report["logical_status"] == "completed"
    assert report["logical_usage"] == final.usage.model_dump(mode="json")
    assert report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
    assert report["actual"]["unmeasured_attempts"] == 0


@pytest.mark.parametrize("until", [0, 4])
def test_recorded_job_finishes_at_zero_or_nonterminal_range_without_claiming_goal_success(
    history, replay_source, until
):
    session, _, commits, _ = replay_source
    manager, _, response = submit(replay_source, until=until)
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    report = manager.replay_status(session.repository_id, UUID(response["job_id"]))["replay"]
    assert report["verified"] and report["last_source_commit"] == commits[until]
    assert report["recomputed_checkpoints"] == report["expected_checkpoints"] == until
    assert report["logical_status"] in ("planning", "running")


def expire(history, job_id):
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )


@pytest.mark.parametrize("stage", ["transaction", "checkpoint", "finish"])
def test_recorded_crash_recovery_keeps_prefix_atomic_and_idempotent(history, replay_source, stage):
    session, _, commits, _ = replay_source
    manager, request, response = submit(replay_source)
    job_id, original_run = UUID(response["job_id"]), UUID(response["run_id"])
    seen = 0

    def crash(point):
        nonlocal seen
        if stage == "transaction" and point == "in_transaction":
            raise RuntimeError("重放证明事务崩溃")
        if stage == "checkpoint" and point == "recorded_after_checkpoint":
            seen += 1
            if seen == 3:
                raise RuntimeError("重放证明提交后崩溃")
        if stage == "finish" and point == "recorded_after_finish":
            raise RuntimeError("重放终态提交后崩溃")

    previous_fault = history.fault
    if stage == "transaction":
        history.fault = crash
    try:
        with pytest.raises(RuntimeError, match="崩溃"):
            Worker(manager.jobs, fault=crash).run_once()
    finally:
        history.fault = previous_fault
    before = manager.jobs.get(session.repository_id, job_id)
    with history.engine.connect() as connection:
        count = connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["replay_checkpoints"])
        ).scalar_one()
    assert count == {"transaction": 0, "checkpoint": 3, "finish": len(commits)}[stage]
    expire(history, job_id)
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    current = manager.jobs.get(session.repository_id, job_id)
    assert (current["run_id"] != original_run) == (stage != "finish")
    assert current["deadline_at"] == before["deadline_at"]
    assert manager.replay(session.repository_id, request, "recorded-submit") == response
    report = manager.replay_status(session.repository_id, job_id)["replay"]
    assert report["verified"] and report["recomputed_checkpoints"] == len(commits) - 1
    assert report["actual"]["unmeasured_attempts"] == 1
    with history.engine.connect() as connection:
        assert connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["replay_checkpoints"])
        ).scalar_one() == len(commits)


@pytest.mark.parametrize("action", ["pause", "cancel", "deadline"])
def test_recorded_job_control_preserves_logical_state_and_resume_keeps_deadline(
    history, replay_source, action
):
    session, _, commits, _ = replay_source
    manager, _, response = submit(replay_source)
    job_id, run_id = UUID(response["job_id"]), UUID(response["run_id"])
    seen = 0

    def control(point):
        nonlocal seen
        if point != "recorded_after_checkpoint":
            return
        seen += 1
        if seen != 3:
            return
        if action == "deadline":
            with history.engine.begin() as connection:
                connection.execute(
                    TABLES["jobs"]
                    .update()
                    .where(TABLES["jobs"].c.id == job_id)
                    .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
                )
        else:
            run = manager.jobs.run(session.repository_id, run_id)
            manager.jobs.request_control(
                session.repository_id, run_id, action, expected_version=run["state_version"]
            )

    assert (
        Worker(manager.jobs, fault=control).run_once()["status"]
        == {"pause": "paused", "cancel": "cancelled", "deadline": "budget_exhausted"}[action]
    )
    before = manager.jobs.get(session.repository_id, job_id)
    report = manager.replay_status(session.repository_id, job_id)["replay"]
    assert not report["verified"] and report["last_source_commit"] == commits[2]
    if action == "pause":
        run = manager.jobs.run(session.repository_id, run_id)
        manager.jobs.request_control(
            session.repository_id, run_id, "resume", expected_version=run["state_version"]
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        after = manager.jobs.get(session.repository_id, job_id)
        assert after["run_id"] == run_id and after["deadline_at"] == before["deadline_at"]
        assert manager.replay_status(session.repository_id, job_id)["replay"]["verified"]


def test_replay_submission_and_all_created_rows_roll_back_with_idempotent_response(
    history, replay_source, monkeypatch
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    request = RecordedReplay(
        source_run_id=str(session.run_id),
        from_commit=commits[0],
        until_commit=commits[-1],
        branch_name="rollback-replay",
    )
    original = history.objects.put

    # 只拦截最终提交响应，确保分支、Task、Job 和请求行一并回滚。
    def response_only(kind, payload, *args, **kwargs):
        if b'"branch_name":"rollback-replay"' in payload and b'"task_id":' in payload:
            raise RuntimeError("重放响应保存失败")
        return original(kind, payload, *args, **kwargs)

    monkeypatch.setattr(history.objects, "put", response_only)
    with pytest.raises(RuntimeError, match="响应保存失败"):
        manager.replay(session.repository_id, request, "rollback-replay")
    with history.engine.connect() as connection:
        for table in ("jobs", "replay_requests", "branch_forks"):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[table])
                ).scalar_one()
                == 0
            )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["tasks"])).scalar_one()
            == 1
        )


def test_two_recorded_jobs_share_content_but_keep_separate_budget_and_proofs(
    history, replay_source
):
    session, final, _, _ = replay_source
    manager, _, first = submit(replay_source, branch="recorded-a", key="replay-a")
    _, _, second = submit(replay_source, branch="recorded-b", key="replay-b")
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    second_result = Worker(manager.jobs).run_once()
    assert second_result["status"] == "completed", (
        second_result,
        manager.events(session.repository_id, UUID(second["run_id"]), 0, 100)["items"][-1],
    )
    rows = [manager.run(session.repository_id, UUID(item["run_id"])) for item in (first, second)]
    assert rows[0]["checkpoint_commit_id"] == rows[1]["checkpoint_commit_id"]
    for row in rows:
        assert row["budget"]["usage"] == final.usage.model_dump(mode="json")
        assert row["replay"]["actual"]["model_calls"] == 0


def test_replay_api_and_cli_share_submission_and_expose_separate_logical_usage(
    history, replay_source, monkeypatch
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    token = "fixture-replay-token-000000000000000000000000"
    request = RecordedReplay(
        source_run_id=str(session.run_id),
        from_commit=commits[0],
        until_commit=commits[4],
        branch_name="entry-replay",
    )
    with TestClient(
        create_app(Settings(api_token=SecretStr(token)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(session.repository_id)},
    ) as client:
        created = client.post(
            "/replays",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": "entry-replay"},
        )
        assert created.status_code == 202, created.text
        response = created.json()
        cli = CliRunner().invoke(
            app,
            [
                "replay",
                str(session.repository_id),
                "--source-run",
                str(session.run_id),
                "--from-commit",
                commits[0],
                "--until-commit",
                commits[4],
                "--branch-name",
                "entry-replay",
                "--key",
                "entry-replay",
            ],
        )
        assert cli.exit_code == 0, cli.stdout
        assert json.loads(cli.stdout) == response
        assert (
            client.post(
                "/replays",
                json={**request.model_dump(mode="json"), "overrides": {}},
                headers={"Idempotency-Key": "invalid"},
            ).status_code
            == 422
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        status = client.get(f"/replays/{response['job_id']}")
        assert status.status_code == 200, status.text
        assert status.json()["replay"]["verified"]
        assert status.json()["replay"]["logical_status"] == "running"
        assert status.json()["replay"]["actual"]["model_calls"] == 0


def test_replay_submission_conflicts_and_cross_repository_fail_without_new_rows(
    history, replay_source
):
    session, _, _, _ = replay_source
    manager, request, _ = submit(replay_source)
    with pytest.raises(AgentGitError) as conflict:
        manager.replay(
            session.repository_id,
            request.model_copy(update={"branch_name": "changed"}),
            "recorded-submit",
        )
    assert conflict.value.code == ErrorCode.VERSION_CONFLICT
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError) as forbidden:
        manager.replay(other, request, "cross-replay")
    assert forbidden.value.code == ErrorCode.NOT_FOUND
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["replay_requests"])
            ).scalar_one()
            == 1
        )


def test_missing_recording_after_enqueue_fails_durably_without_live_fallback(
    history, replay_source, monkeypatch
):
    session, _, _, load = replay_source
    recording = load().recordings[0]
    manager, request, response = submit(replay_source)
    reference = recording.saved.response_ref
    raw = history.objects.get(reference, ObjectType.BLOB)
    path = history.objects.path_for(reference)
    assert path.resolve().is_relative_to(history.objects.root.resolve())
    path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("缺录制不能回退到真实调用")

    try:
        with monkeypatch.context() as offline:
            offline.setattr("agentgit.adapters.tools.ToolRegistry.invoke", forbidden)
            offline.setattr("agentgit.adapters.execution.engine.DockerEngine.__init__", forbidden)
            result = Worker(manager.jobs, model_factory=forbidden).run_once()
        assert result["status"] == "failed" and result["error_code"] == "recording_missing"
        report = manager.replay_status(session.repository_id, UUID(response["job_id"]))["replay"]
        assert not report["verified"] and not report["restored"]
        assert report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
        assert manager.replay(session.repository_id, request, "recorded-submit") == response
    finally:
        history.objects.put(ObjectType.BLOB, raw)


def test_replay_request_and_proofs_are_immutable_and_source_order_is_guarded(
    history, replay_source
):
    session, _, _, _ = replay_source
    manager, _, response = submit(replay_source)
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    job_id = UUID(response["job_id"])
    requests, points = TABLES["replay_requests"], TABLES["replay_checkpoints"]
    statements = (
        requests.update().where(requests.c.job_id == job_id).values(mode="live"),
        requests.delete().where(requests.c.job_id == job_id),
        points.update().where(points.c.job_id == job_id).values(actual_tool_calls=1),
        points.delete().where(points.c.job_id == job_id),
    )
    for statement in statements:
        with pytest.raises(sa.exc.DBAPIError), history.engine.begin() as connection:
            connection.execute(statement)
    with history.engine.connect() as connection:
        last = dict(
            connection.execute(
                sa.select(points)
                .where(points.c.job_id == job_id)
                .order_by(points.c.source_order.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )
    with pytest.raises(sa.exc.DBAPIError), history.engine.begin() as connection:
        connection.execute(
            points.insert().values(**{**last, "source_order": last["source_order"] + 2})
        )
