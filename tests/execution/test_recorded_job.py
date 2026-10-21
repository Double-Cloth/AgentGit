"""真实五工具来源通过耐久 Recorded Job 重算；不能初始化执行客户端。"""

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


@pytest.mark.parametrize("missing_output", [False, True])
def test_durable_recorded_job_from_real_five_tools_has_zero_new_calls_or_fails_missing_output(
    history, tmp_path, image_id, monkeypatch, missing_output
):
    queue, repository, source = submit(history, tmp_path)
    assert Worker(queue).run_once()["status"] == "completed"
    versions = VersionService(history)
    commits = [item["commit_id"] for item in reversed(versions.log(repository)["items"])]
    original = history.branch(repository)
    manager = ManagementService(queue.research)
    replay = manager.replay(
        repository,
        RecordedReplay(
            source_run_id=source.run_id,
            from_commit=commits[0],
            until_commit=commits[-1],
            branch_name="recorded-five-tools",
        ),
        "recorded-five-tools",
    )
    removed = None
    if missing_output:
        snapshot = history.restore(repository, commits[-1])
        removed = next(
            item.object_ref for item in snapshot.artifacts if item.path == "results/statistics.json"
        )
        raw = history.objects.get(removed, ObjectType.BLOB)
        path = history.objects.path_for(removed)
        assert path.resolve().is_relative_to(history.objects.root.resolve())
        path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("耐久 Recorded Job 不能初始化真实模型、工具或容器客户端")

    try:
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
        assert result["status"] == ("failed" if missing_output else "completed"), result
        report = manager.replay_status(repository, UUID(replay["job_id"]))["replay"]
        assert report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
        if missing_output:
            assert result["error_code"] == "recording_missing" and not report["verified"]
        else:
            assert report["verified"] and report["recomputed_checkpoints"] == 28
            current = manager.jobs.run(repository, UUID(replay["run_id"]))
            assert history.restore(repository, current["checkpoint_commit_id"]) == history.restore(
                repository, commits[-1]
            )
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["external_calls"])
                ).scalar_one()
                == 28
            )
        assert history.branch(repository) == original
    finally:
        if removed is not None:
            history.objects.put(ObjectType.BLOB, raw)
