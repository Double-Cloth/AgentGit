"""真实终端/Python 录制经原 Runtime 重算，完全不构造执行后端。"""

from uuid import UUID

import pytest
from test_agent_flow import submit

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RecordedReplayer
from agentgit.services.recordings import RecordingReader
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("missing_output", [False, True])
def test_recorded_runtime_rebuilds_real_execution_without_engine_or_interpreter(
    history, tmp_path, image_id, monkeypatch, missing_output
):
    queue, repository, job = submit(history, tmp_path)
    assert Worker(queue).run_once()["status"] == "completed"
    versions = VersionService(history)
    commits = tuple(item["commit_id"] for item in reversed(versions.log(repository)["items"]))
    environment = parse_contract(
        history.objects.get(
            versions.checkout(repository, commits[0])["manifest"]["environment_ref"],
            ObjectType.ENVIRONMENT,
        ),
        EnvironmentManifest,
    )
    bundle = RecordingReader(history).load(
        repository,
        RecordedRange(source_run_id=job.run_id, from_commit=commits[0], until_commit=commits[-1]),
        expected_environment=environment,
    )
    payload = queue.payload(queue.get(repository, UUID(job.job_id)))
    config = queue.research.config(repository, UUID(payload.config_id))
    assert isinstance(config, AgentConfig)
    original = history.branch(repository)
    removed = None
    if missing_output:
        artifact = next(
            item
            for item in bundle.checkpoints[-1][1].artifacts
            if item.path == "results/statistics.json"
        )
        removed = artifact.object_ref
        raw = history.objects.get(removed, ObjectType.BLOB)
        path = history.objects.path_for(removed)
        assert path.resolve().is_relative_to(history.objects.root.resolve())
        path.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Recorded 重算不能调用网络、容器、宿主进程或真实 Adapter")

    try:
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
            if missing_output:
                with pytest.raises(AgentGitError) as error:
                    RecordedReplayer(history.objects.get).run(bundle, config, queue.research.corpus)
                assert error.value.code == ErrorCode.RECORDING_MISSING
            else:
                result = RecordedReplayer(history.objects.get).run(
                    bundle, config, queue.research.corpus
                )
                assert result.consumed_calls == 28 and len(result.checkpoints) == 28
                assert result.actual_model_calls == result.actual_tool_calls == 0
                assert result.checkpoints[-1].snapshot == bundle.checkpoints[-1][1]
                assert result.state.usage.tool_calls == 13 and result.state.usage.model_calls == 15
        assert history.branch(repository) == original
    finally:
        if removed is not None:
            history.objects.put(ObjectType.BLOB, raw)
