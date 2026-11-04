"""MCP 经真实 Worker、审批、独立分支及 Recorded 的完整单次任务。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from mcp_evidence import archive_mcp_run as archive_mcp_run
from test_mcp_adapter import server as server
from test_merge_process_crash import kill_interpreter

from agentgit.adapters.mcp import MCPClient
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import MCPArguments
from agentgit.core.mcp_runs import MCPProgram, MCPRecordedSpec, MCPRunState
from agentgit.core.models import CallIntent, EffectClass, parse_contract
from agentgit.core.versions import NewBranch
from agentgit.services.approvals import ApprovalService
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.mcp_runs import MCPRunService
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_worker_credentials(monkeypatch):
    monkeypatch.delenv("AGENTGIT_MCP_TRUSTED_FIXTURE_TOKEN", raising=False)


def case(history, server, tmp_path, effect=EffectClass.READ_ONLY, *, lease_ms=1000):
    config, counts, mode = server
    binding = MCPClient(config).bind("echo", effect)
    program = MCPProgram(binding=binding, request=MCPArguments(arguments={"value": "task-value"}))
    research = ResearchService(history, tmp_path / "data")
    queue = JobService(research) if lease_ms is None else JobService(research, lease_ms=lease_ms)
    service = MCPRunService(queue)
    repo = history.create_repository("MCP 有界任务")
    submitted = service.submit(
        repo,
        program,
        actor="mcp-operator",
        key="live-once",
        branch_name="main",
        expected_head=None,
        expected_version=0,
    )
    return queue, service, repo, program, submitted, counts, mode


def state(history, repo, run):
    with history.engine.connect() as connection:
        row = dict(history._run(connection, repo, run))
    snapshot = history.restore(repo, row["checkpoint_commit_id"])
    return parse_contract(encode(snapshot.data), MCPRunState), snapshot, row


def approve(history, queue, repo, run, program):
    approvals = ApprovalService(
        history, ToolPolicy(rules=(program.binding.rule(),)), actor="mcp-operator"
    )
    pending = approvals.list_for_run(repo, run)[-1]
    row = queue.run(repo, run)
    return approvals.decide(
        repo,
        pending["id"],
        actor="mcp-operator",
        decision="approve",
        expected_version=row["state_version"],
    )


@pytest.mark.parametrize("effect", [EffectClass.READ_ONLY, EffectClass.NON_IDEMPOTENT_WRITE])
def test_worker_success_and_recorded_preserve_artifact_and_logical_projection(
    history, server, tmp_path, effect
):
    queue, service, repo, program, submitted, counts, _ = case(history, server, tmp_path, effect)
    run = UUID(submitted.run_id)
    result = Worker(queue).run_once()
    if effect != EffectClass.READ_ONLY:
        assert result["status"] == "waiting_approval" and counts()["calls"] == 0
        approve(history, queue, repo, run, program)
        result = Worker(queue).run_once()
    assert result["status"] == "completed"
    original, source_snapshot, source_row = state(history, repo, run)
    assert original.usage.tool_calls == 1 and original.actual_attempts == 1
    assert (
        len(source_snapshot.artifacts) == 1
        and source_snapshot.artifacts[0].path == "mcp-result.json"
    )
    assert source_snapshot.artifacts[0].object_ref == original.response_ref
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    VersionService(history).fork(
        repo,
        NewBranch(
            source_run_id=str(run),
            from_commit=intent.checkpoint,
            name="mcp-recorded",
            expected_version=0,
        ),
        actor="mcp-operator",
    )
    replay = MCPRecordedSpec(
        source_run_id=str(run),
        from_commit=intent.checkpoint,
        to_commit=source_row["checkpoint_commit_id"],
    )
    target = service.submit(
        repo,
        program,
        actor="mcp-operator",
        key="recorded-once",
        branch_name="mcp-recorded",
        expected_head=intent.checkpoint,
        expected_version=0,
        replay=replay,
    )
    before = counts()

    def forbidden_client(_):
        pytest.fail("Recorded 不得构造 MCP 客户端")

    result = Worker(queue, mcp_client_factory=forbidden_client).run_once()
    assert result["status"] == "completed" and counts() == before
    projected, target_snapshot, _ = state(history, repo, UUID(target.run_id))
    assert projected.logical_projection() == original.logical_projection()
    assert projected.actual_attempts == 0
    assert target_snapshot.artifacts == source_snapshot.artifacts
    assert state(history, repo, run)[2] == source_row


def test_queued_cancel_does_not_construct_client_or_call(history, server, tmp_path):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    queue.request_control(
        repo, run, "cancel", expected_version=queue.run(repo, run)["state_version"]
    )
    before = counts()
    result = Worker(
        queue, mcp_client_factory=lambda _: pytest.fail("取消不能创建客户端")
    ).run_once()
    assert result["status"] == "cancelled" and counts() == before
    current, snapshot, _ = state(history, repo, run)
    assert current.actual_attempts == 0 and current.usage.tool_calls == 0 and not snapshot.artifacts
    assert history.branch(repo)["active_writer_run_id"] is None


def test_recorded_mode_without_fixed_recording_fails_before_client(history, server, tmp_path):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["runs"].update().where(TABLES["runs"].c.id == run).values(mode="recorded")
        )
    before = counts()
    result = Worker(
        queue, mcp_client_factory=lambda _: pytest.fail("Recorded 模式不能进入 Live 客户端")
    ).run_once()
    assert result["status"] == "failed" and result["error_code"] == "replay_mismatch"
    assert counts() == before


def test_queued_pause_and_resume_execute_only_once(history, server, tmp_path):
    # 功能用例使用生产默认租约；短租约失效、心跳与 fencing 由专门用例核对。
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path, lease_ms=None)
    run = UUID(submitted.run_id)
    queue.request_control(
        repo, run, "pause", expected_version=queue.run(repo, run)["state_version"]
    )
    paused = Worker(
        queue, mcp_client_factory=lambda _: pytest.fail("排队暂停不能创建客户端")
    ).run_once()
    assert paused["status"] == "paused" and counts()["calls"] == 0, paused
    queue.request_control(
        repo, run, "resume", expected_version=queue.run(repo, run)["state_version"]
    )
    resumed = Worker(queue).run_once()
    assert resumed["status"] == "completed" and counts()["calls"] == 1, resumed
    assert queue.run(repo, run)["control_status"] == "applied"


def test_submission_key_and_branch_writer_cannot_be_rebound(history, server, tmp_path):
    _, service, repo, program, submitted, counts, _ = case(history, server, tmp_path)
    assert (
        service.submit(
            repo,
            program,
            actor="mcp-operator",
            key="live-once",
            branch_name="main",
            expected_head=None,
            expected_version=0,
        )
        == submitted
    )
    other = program.model_copy(update={"request": MCPArguments(arguments={"value": "different"})})
    for key, value in [("live-once", other), ("second", program)]:
        with pytest.raises(AgentGitError) as caught:
            service.submit(
                repo,
                value,
                actor="mcp-operator",
                key=key,
                branch_name="main",
                expected_head=None,
                expected_version=0,
            )
        assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert counts()["calls"] == 0


@pytest.mark.parametrize("mode", ["drop", "timeout"])
def test_unknown_does_not_requeue_or_reissue_after_cancel(history, server, tmp_path, mode):
    queue, _, repo, program, submitted, counts, set_mode = case(
        history, server, tmp_path, EffectClass.NON_IDEMPOTENT_WRITE
    )
    run = UUID(submitted.run_id)
    assert Worker(queue).run_once()["status"] == "waiting_approval"
    approve(history, queue, repo, run, program)
    set_mode(mode)
    assert Worker(queue).run_once()["status"] == "outcome_unknown"
    current, _, row = state(history, repo, run)
    assert current.pending and current.actual_attempts is None
    assert current.error_code == ErrorCode.OUTCOME_UNKNOWN.value
    queue.request_control(repo, run, "cancel", expected_version=row["state_version"])
    before = counts()
    assert Worker(queue).run_once() is None
    assert counts() == before and counts()["calls"] == 1


def test_expired_job_has_zero_calls_even_when_resumed(history, server, tmp_path):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    queue.request_control(
        repo, run, "pause", expected_version=queue.run(repo, run)["state_version"]
    )
    assert Worker(queue).run_once()["status"] == "paused"
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
            .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    queue.request_control(
        repo, run, "resume", expected_version=queue.run(repo, run)["state_version"]
    )
    result = Worker(
        queue, mcp_client_factory=lambda _: pytest.fail("耗尽期限不能创建客户端")
    ).run_once()
    assert result["status"] == "budget_exhausted"
    assert result["error_code"] == ErrorCode.BUDGET_EXHAUSTED.value and counts()["calls"] == 0


@pytest.mark.parametrize("point", ["mcp.after_intent", "mcp.after_result", "mcp.after_observation"])
def test_actual_worker_process_death_never_reissues_write(history, server, tmp_path, point):
    queue, _, repo, program, submitted, counts, _ = case(
        history, server, tmp_path, EffectClass.NON_IDEMPOTENT_WRITE
    )
    run = UUID(submitted.run_id)
    assert Worker(queue).run_once()["status"] == "waiting_approval"
    approve(history, queue, repo, run, program)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "worker-boundary.json"
    env = os.environ.copy()
    env.update(
        AGENTGIT_MCP_TEST_SCHEMA=schema,
        AGENTGIT_MCP_TEST_POINT=point,
        AGENTGIT_MCP_TEST_DATABASE=history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_MCP_TEST_OBJECTS=str(history.objects.root),
        AGENTGIT_MCP_TEST_DATA=str(tmp_path / "data"),
        AGENTGIT_MCP_TEST_MARKER=str(marker),
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).parents[1] / "helpers/mcp_worker_process.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = monotonic() + 20
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode("utf-8", errors="replace"))
            assert monotonic() < deadline, "实际 Worker 未抵达所选耐久边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text("utf-8"))
        assert boundary["point"] == point and boundary["schema"] == schema
        assert boundary["pid"] == process.pid or boundary["parent_pid"] == process.pid
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.terminate()
        process.wait(timeout=10)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()
    before = counts()
    previous_token = queue.get(repo, UUID(submitted.job_id))["fencing_token"]
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    result = Worker(
        queue, mcp_client_factory=lambda _: pytest.fail("恢复不能重建已决或未知动作的客户端")
    ).run_once()
    assert result["status"] == ("outcome_unknown" if point == "mcp.after_intent" else "completed")
    assert counts() == before and counts()["calls"] == (0 if point == "mcp.after_intent" else 1)
    new_token = queue.get(repo, UUID(submitted.job_id))["fencing_token"]
    assert new_token > previous_token
    (tmp_path / "worker-death.json").write_text(
        json.dumps(
            {
                "boundary": boundary,
                "launcher_pid": process.pid,
                "exit_code": process.returncode,
                "old_token": previous_token,
                "new_token": new_token,
                "recovered": result,
                "requests_unchanged": counts() == before,
            }
        ),
        encoding="utf-8",
    )


def test_lost_lease_between_discovery_and_call_prevents_write(history, server, tmp_path):
    queue, _, repo, program, submitted, counts, _ = case(
        history, server, tmp_path, EffectClass.NON_IDEMPOTENT_WRITE
    )
    run = UUID(submitted.run_id)
    assert Worker(queue).run_once()["status"] == "waiting_approval"
    approve(history, queue, repo, run, program)

    class FencedClient(MCPClient):
        def _rpc(self, method, params):
            result = super()._rpc(method, params)
            if method == "tools/list":
                with history.engine.begin() as connection:
                    jobs = TABLES["jobs"]
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.id == UUID(submitted.job_id))
                        .values(fencing_token=jobs.c.fencing_token + 1, worker="other-worker")
                    )
            return result

    result = Worker(
        queue, mcp_client_factory=lambda binding: FencedClient(binding.server)
    ).run_once()
    assert result["status"] == "lease_lost" and counts()["calls"] == 0
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    before = counts()
    assert (
        Worker(queue, mcp_client_factory=lambda _: pytest.fail("失效意图不能重发")).run_once()[
            "status"
        ]
        == "outcome_unknown"
    )
    assert counts() == before


def test_missing_recorded_response_fails_without_live_fallback(history, server, tmp_path):
    queue, service, repo, program, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    assert Worker(queue).run_once()["status"] == "completed"
    original, _, row = state(history, repo, run)
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    VersionService(history).fork(
        repo,
        NewBranch(
            source_run_id=str(run),
            from_commit=intent.checkpoint,
            name="missing-recorded",
            expected_version=0,
        ),
        actor="mcp-operator",
    )
    replay = MCPRecordedSpec(
        source_run_id=str(run), from_commit=intent.checkpoint, to_commit=row["checkpoint_commit_id"]
    )
    service.submit(
        repo,
        program,
        actor="mcp-operator",
        key="missing-recorded",
        branch_name="missing-recorded",
        expected_head=intent.checkpoint,
        expected_version=0,
        replay=replay,
    )
    path = history.objects.path_for(original.response_ref)
    assert path.resolve().is_relative_to(history.objects.root.resolve())
    raw = path.read_bytes()
    path.unlink()
    before = counts()
    try:
        result = Worker(
            queue, mcp_client_factory=lambda _: pytest.fail("缺录制不能回退 Live")
        ).run_once()
        assert result["status"] == "failed" and counts() == before
    finally:
        path.write_bytes(raw)


def test_recorded_source_cannot_cross_repository(history, server, tmp_path):
    queue, service, repo, program, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    assert Worker(queue).run_once()["status"] == "completed"
    _, _, row = state(history, repo, run)
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    other = history.create_repository("MCP 另一项目")
    before = counts()
    with pytest.raises(AgentGitError) as caught:
        service.submit(
            other,
            program,
            actor="mcp-operator",
            key="foreign-recorded",
            branch_name="main",
            expected_head=None,
            expected_version=0,
            replay=MCPRecordedSpec(
                source_run_id=str(run),
                from_commit=intent.checkpoint,
                to_commit=row["checkpoint_commit_id"],
            ),
        )
    assert caught.value.code == ErrorCode.NOT_FOUND and counts() == before
