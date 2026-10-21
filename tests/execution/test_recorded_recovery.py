"""崩溃来源的已保存调用必须按原恢复结算顺序重算。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_agent_flow import submit
from test_stopped_retries import expire

from agentgit.core.canonical import encode
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RecordedReplayer
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
