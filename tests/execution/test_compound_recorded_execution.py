"""真实执行录制经过两层证明后仍零启动；删除底层产物必须失败。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_agent_flow import submit

from agentgit.core.canonical import ObjectType
from agentgit.core.versions import RecordedReplay
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("missing", [False, True])
def test_compound_real_execution_uses_original_evidence_without_container_backend(
    history, tmp_path, image_id, monkeypatch, missing
):
    queue, repository, source = submit(history, tmp_path)
    assert Worker(queue).run_once()["status"] == "completed"
    original = history.branch(repository)
    chain = VersionService(history).log(repository)["items"]
    manager = ManagementService(queue.research)
    first = manager.replay(
        repository,
        RecordedReplay(
            source_run_id=source.run_id,
            from_commit=chain[-1]["commit_id"],
            until_commit=chain[0]["commit_id"],
            branch_name="execution-first",
        ),
        "execution-first",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    with history.engine.connect() as connection:
        proofs = list(
            connection.execute(
                sa.select(TABLES["replay_checkpoints"])
                .where(TABLES["replay_checkpoints"].c.job_id == UUID(first["job_id"]))
                .order_by(TABLES["replay_checkpoints"].c.source_order)
            ).mappings()
        )
    nested = manager.replay(
        repository,
        RecordedReplay(
            source_run_id=first["run_id"],
            from_commit=proofs[0]["commit_id"],
            until_commit=proofs[-1]["commit_id"],
            branch_name="execution-compound",
        ),
        "execution-compound",
    )
    removed = None
    if missing:
        snapshot = history.restore(repository, chain[0]["commit_id"])
        removed = next(
            item.object_ref for item in snapshot.artifacts if item.path == "results/statistics.json"
        )
        raw = history.objects.get(removed, ObjectType.BLOB)
        path = history.objects.path_for(removed)
        assert path.resolve().is_relative_to(history.objects.root.resolve())
        path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("复合执行 Recorded 不能初始化真实模型、工具、工作区或容器")

    try:
        with monkeypatch.context() as offline:
            for entry in (
                "agentgit.adapters.mock.MockResearchModel.invoke",
                "agentgit.adapters.tools.ToolRegistry.invoke",
                "subprocess.Popen",
                "socket.create_connection",
                "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
                "agentgit.adapters.execution.engine.DockerEngine.__init__",
                "agentgit.adapters.execution.container.SandboxContainer.__init__",
            ):
                offline.setattr(entry, forbidden)
            result = Worker(manager.jobs, model_factory=forbidden).run_once()
        assert result["status"] == ("failed" if missing else "completed"), result
        if missing:
            assert result["error_code"] == "recording_missing"
        report = manager.replay_status(repository, UUID(nested["job_id"]))["replay"]
        assert report["verified"] == (not missing)
        assert report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
                ).scalar_one()
                == 2
            )
        assert history.branch(repository) == original
    finally:
        if removed is not None:
            history.objects.put(ObjectType.BLOB, raw)
