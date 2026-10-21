"""真实五工具录制的离线读取，不得构造容器后端或漏验输出对象。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_agent_flow import submit
from test_stopped_retries import crash_after_execution, expire

from agentgit.adapters.execution.container import SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import ExecutionManifest, ExecutionResult
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.versions import NewBranch, RecordedRange
from agentgit.replay.recorded import RecordedAdapter
from agentgit.services.history import TABLES
from agentgit.services.recordings import RecordingReader
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("missing", [None, "response", "output"])
def test_read_complete_execution_recordings_without_docker_or_fail_on_missing_assets(
    history, tmp_path, image_id, monkeypatch, missing
):
    queue, repository, job = submit(history, tmp_path)
    assert Worker(queue).run_once()["status"] == "completed"
    versions = VersionService(history)
    commits = [item["commit_id"] for item in reversed(versions.log(repository)["items"])]
    environment = parse_contract(
        encode(versions.checkout(repository, commits[0])["environment"]), EnvironmentManifest
    )
    request = RecordedRange(
        source_run_id=job.run_id, from_commit=commits[0], until_commit=commits[-1]
    )

    def no_backend(*args, **kwargs):
        pytest.fail("录制读取不能构造执行后端或连接 Docker")

    monkeypatch.setattr(DockerEngine, "local", no_backend)
    monkeypatch.setattr(SandboxContainer, "__init__", no_backend)
    bundle = RecordingReader(history).load(repository, request, expected_environment=environment)
    assert len(bundle.recordings) == 28 and len(bundle.checkpoints) == 29
    if missing is not None:
        if missing == "response":
            reference = bundle.recordings[0].saved.response_ref
        else:
            terminal = next(
                item
                for item in bundle.recordings
                if item.intent.occurrence.provider == "tool:terminal_exec/v1"
            )
            result = parse_contract(terminal.response, ExecutionResult)
            outputs = parse_contract(
                history.objects.get(result.output_manifest_ref, ObjectType.ARTIFACT_MANIFEST),
                ExecutionManifest,
            )
            reference = next(
                item.object_ref for item in outputs.files if item.path == "results/statistics.json"
            )
        path = history.objects.root / reference[:2] / reference[2:]
        assert path.resolve().is_relative_to(history.objects.root.resolve())
        raw = path.read_bytes()
        path.unlink()
        try:
            with pytest.raises(AgentGitError) as error:
                RecordingReader(history).load(repository, request, expected_environment=environment)
            assert error.value.code == ErrorCode.RECORDING_MISSING
        finally:
            history.objects.put(ObjectType.BLOB, raw)
        return
    adapter = RecordedAdapter(bundle.recordings, lambda: None)
    for record in bundle.recordings:
        result = adapter.take(
            record.intent.occurrence,
            record.intent.effect,
            record.request,
            checkpoint=record.intent.checkpoint,
            max_bytes=record.intent.max_response_bytes,
        )
        assert result.trace.response_ref == record.saved.response_ref
    adapter.assert_consumed()


def test_confirmed_discard_checkpoint_is_safe_to_fork_without_inheriting_old_container(
    history, tmp_path, image_id, monkeypatch
):
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    expire(queue, job)
    assert Worker(queue).run_once()["status"] == "completed"
    current = queue.get(repository, UUID(job.job_id))
    with history.engine.connect() as connection:
        checkpoint = connection.execute(
            sa.select(TABLES["snapshots"].c.commit_id)
            .join(
                TABLES["events"],
                sa.and_(
                    TABLES["events"].c.run_id == TABLES["snapshots"].c.run_id,
                    TABLES["events"].c.seq == TABLES["snapshots"].c.event_seq,
                ),
            )
            .where(
                TABLES["events"].c.run_id == current["run_id"],
                TABLES["events"].c.type == "sandbox.execution_discarded",
            )
        ).scalar_one()
    before = history.branch(repository)

    def no_backend(*args, **kwargs):
        pytest.fail("Fork 检查不能重启旧容器")

    monkeypatch.setattr(SandboxContainer, "__init__", no_backend)
    created = VersionService(history).fork(
        repository,
        NewBranch(
            source_run_id=str(current["run_id"]),
            from_commit=checkpoint,
            name="after-discard",
            expected_version=0,
        ),
        actor="local-operator",
    )
    assert created["head_commit_id"] == checkpoint
    assert history.branch(repository) == before
