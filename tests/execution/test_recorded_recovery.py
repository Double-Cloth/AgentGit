"""崩溃来源的已保存调用必须按原恢复结算顺序重算。"""

from dataclasses import replace
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_agent_flow import submit
from test_stopped_retries import crash_after_execution, expire, make_cleanup_available

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import BudgetLimits
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RecordedReplayer
from agentgit.services.executions import ExecutionService
from agentgit.services.history import TABLES
from agentgit.services.recordings import RecordingReader
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("control", [None, "cancel", "deadline"])
def test_saved_result_recovery_is_replayed_before_source_control_without_new_execution(
    history, tmp_path, image_id, monkeypatch, control
):
    queue, repository, job = submit(history, tmp_path)
    crashed = False

    def crash(point):
        nonlocal crashed
        if point != "after_result" or crashed:
            return
        with history.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["sandbox_executions"])
                .where(TABLES["sandbox_executions"].c.status == "complete")
            ).scalar_one()
        if count == 1:
            crashed = True
            raise RuntimeError("完整结果已保存、尚未观察")

    with pytest.raises(RuntimeError, match="尚未观察"):
        Worker(queue, fault=crash).run_once()
    if control == "cancel":
        before = queue.run(repository, UUID(job.run_id))
        queue.request_control(
            repository, UUID(job.run_id), "cancel", expected_version=before["state_version"]
        )
    expire(queue, job, deadline=control == "deadline")
    result = Worker(queue).run_once()
    assert (
        result["status"]
        == {None: "completed", "cancel": "cancelled", "deadline": "budget_exhausted"}[control]
    )
    current = queue.get(repository, UUID(job.job_id))
    source = queue.run(repository, current["run_id"])
    versions = VersionService(history)
    view = versions.checkout(repository, source["base_commit_id"])
    environment = parse_contract(encode(view["environment"]), EnvironmentManifest)
    bundle = RecordingReader(history).load(
        repository,
        RecordedRange(
            source_run_id=str(current["run_id"]),
            from_commit=source["base_commit_id"],
            until_commit=source["checkpoint_commit_id"],
        ),
        expected_environment=environment,
    )
    assert bundle.recordings[0].reused
    payload = queue.payload(current)
    config = queue.research.config(repository, UUID(payload.config_id))

    def forbidden(*args, **kwargs):
        pytest.fail("恢复录制不能触发真实模型、工具、网络、执行环境")

    with monkeypatch.context() as offline:
        for entry in (
            "socket.socket",
            "socket.create_connection",
            "subprocess.Popen",
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
            "agentgit.adapters.execution.container.SandboxContainer.__init__",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.tools.ToolRegistry.invoke",
        ):
            offline.setattr(entry, forbidden)
        replay = RecordedReplayer(history.objects.get).run(bundle, config, queue.research.corpus)
    assert replay.checkpoints[-1].snapshot == bundle.checkpoints[-1][1]
    assert replay.state.usage.tool_calls == (11 if control else 13)
    assert replay.actual_model_calls == replay.actual_tool_calls == 0


@pytest.mark.parametrize("stop", [None, "cancel", "deadline", "tool_budget", "attempt_limit"])
def test_confirmed_discard_is_recomputed_once_before_retry_or_source_stop(
    history, tmp_path, image_id, monkeypatch, stop
):
    limits = BudgetLimits(max_tool_calls=11) if stop == "tool_budget" else None
    queue, repository, job = submit(history, tmp_path, limits=limits)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    if stop == "cancel":
        before = queue.run(repository, UUID(job.run_id))
        queue.request_control(
            repository, UUID(job.run_id), "cancel", expected_version=before["state_version"]
        )
    expire(queue, job, deadline=stop == "deadline")
    if stop == "attempt_limit":
        with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
            Worker(queue, fault=crash_after_execution).run_once()
        expire(queue, job)
    result = Worker(queue).run_once()
    expected = {
        None: "completed",
        "cancel": "cancelled",
        "deadline": "budget_exhausted",
        "tool_budget": "budget_exhausted",
        "attempt_limit": "budget_exhausted",
    }
    assert result["status"] == expected[stop], result
    current = queue.get(repository, UUID(job.job_id))
    source = queue.run(repository, current["run_id"])
    versions = VersionService(history)
    environment = parse_contract(
        encode(versions.checkout(repository, source["base_commit_id"])["environment"]),
        EnvironmentManifest,
    )
    request = RecordedRange(
        source_run_id=str(current["run_id"]),
        from_commit=source["base_commit_id"],
        until_commit=source["checkpoint_commit_id"],
    )
    bundle = RecordingReader(history).load(repository, request, expected_environment=environment)
    assert len(bundle.discards) == 1
    config = queue.research.config(repository, UUID(queue.payload(current).config_id))
    original = history.branch(repository)

    def forbidden(*args, **kwargs):
        pytest.fail("丢弃重算不能初始化容器或真实 Adapter")

    with monkeypatch.context() as offline:
        for entry in (
            "socket.socket",
            "socket.create_connection",
            "subprocess.Popen",
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
            "agentgit.adapters.execution.container.SandboxContainer.__init__",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.tools.ToolRegistry.invoke",
        ):
            offline.setattr(entry, forbidden)
        replay = RecordedReplayer(history.objects.get).run(bundle, config, queue.research.corpus)
    assert replay.checkpoints[-1].snapshot == bundle.checkpoints[-1][1]
    assert len(replay.state.stopped_executions) == (2 if stop == "attempt_limit" else 1)
    assert replay.state.usage.tool_calls == (
        14 if stop is None else 12 if stop == "attempt_limit" else 11
    )
    assert replay.actual_model_calls == replay.actual_tool_calls == 0
    assert history.branch(repository) == original


def test_unknown_source_discard_needs_complete_proof_and_rejects_forged_input(
    history, tmp_path, image_id, monkeypatch
):
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    expire(queue, job)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(ExecutionService, "reconcile", staticmethod(lambda *args: 0))
        assert Worker(queue).run_once()["status"] == "outcome_unknown"
    make_cleanup_available(queue, job)
    assert Worker(queue).run_once()["status"] == "completed"
    current = queue.get(repository, UUID(job.job_id))
    source = queue.run(repository, current["run_id"])
    environment = parse_contract(
        encode(
            VersionService(history).checkout(repository, source["base_commit_id"])["environment"]
        ),
        EnvironmentManifest,
    )
    request = RecordedRange(
        source_run_id=str(current["run_id"]),
        from_commit=source["base_commit_id"],
        until_commit=source["checkpoint_commit_id"],
    )
    bundle = RecordingReader(history).load(repository, request, expected_environment=environment)
    assert bundle.checkpoints[0][1].data["status"] == "outcome_unknown"
    assert len(bundle.discards) == 1
    proof = bundle.discards[0].event.cleanup_evidence_ref
    raw = history.objects.get(proof, ObjectType.BLOB)
    path = history.objects.path_for(proof)
    assert path.resolve().is_relative_to(history.objects.root.resolve())
    path.unlink()
    try:
        with pytest.raises(AgentGitError) as missing:
            RecordingReader(history).load(repository, request, expected_environment=environment)
        assert missing.value.code == ErrorCode.RECORDING_MISSING
    finally:
        history.objects.put(ObjectType.BLOB, raw)
    original = bundle.discards[0]
    certificate = original.event.stopped_execution.model_copy(
        update={"input_manifest_ref": "a" * 64}
    )
    event = original.event.model_copy(update={"stopped_execution": certificate})
    forged = replace(bundle, discards=(replace(original, event=event),))
    config = queue.research.config(repository, UUID(queue.payload(current).config_id))

    def forbidden(*args, **kwargs):
        pytest.fail("未知来源的录制重算不能调用真实能力")

    with monkeypatch.context() as offline:
        for entry in (
            "socket.socket",
            "socket.create_connection",
            "subprocess.Popen",
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
            "agentgit.adapters.execution.container.SandboxContainer.__init__",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.tools.ToolRegistry.invoke",
        ):
            offline.setattr(entry, forbidden)
        with pytest.raises(AgentGitError) as mismatch:
            RecordedReplayer(history.objects.get).run(forged, config, queue.research.corpus)
        assert mismatch.value.code == ErrorCode.REPLAY_MISMATCH
        replay = RecordedReplayer(history.objects.get).run(bundle, config, queue.research.corpus)
    assert replay.checkpoints[-1].snapshot == bundle.checkpoints[-1][1]
    assert replay.state.usage.tool_calls == 14
    assert replay.actual_model_calls == replay.actual_tool_calls == 0
