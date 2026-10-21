"""S5-T06：同一可信 fixture 绑定下完整重算外部写，绝不访问服务。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_approvals import effect_server as effect_server
from test_reconciliation import confirm
from test_reconciliation import unknown as unknown

from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass, EnvironmentManifest, parse_contract
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RecordedReplayer
from agentgit.services.history import TABLES
from agentgit.services.recordings import RecordingReader
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("unknown", [None, "crash_before_stop"], indirect=True)
@pytest.mark.parametrize("cancel", [False, True])
def test_full_runtime_replay_of_confirmed_external_write_is_offline(
    history, unknown, monkeypatch, cancel
):
    queue, repo, source, job, _, worker, counts, _ = unknown
    if cancel:
        run = queue.run(repo, source)
        queue.request_control(repo, source, "cancel", expected_version=run["state_version"])
    confirm(unknown)
    result = worker().run_once()
    assert result["status"] == ("cancelled" if cancel else "completed"), result
    source = UUID(result["run_id"])
    head = queue.get(repo, job)["checkpoint_commit_id"]
    run = queue.run(repo, source)
    with history.engine.connect() as connection:
        commits = list(
            connection.execute(
                sa.select(TABLES["run_commits"].c.commit_id)
                .where(
                    TABLES["run_commits"].c.run_id == source,
                    TABLES["run_commits"].c.repository_id == repo,
                )
                .order_by(TABLES["run_commits"].c.run_order)
            ).scalars()
        )
    start = run["base_commit_id"] or commits[0]
    environment = parse_contract(
        encode(VersionService(history).checkout(repo, head)["environment"]), EnvironmentManifest
    )
    config = queue.research.config(repo, run["agent_config_id"])
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(source_run_id=str(source), from_commit=start, until_commit=head),
        expected_environment=environment,
    )
    assert len(bundle.settlements) == 1
    assert len(bundle.unknown_stops) == int(run["base_commit_id"] is not None)
    # 生产默认 Registry 不会依据录制中的声明授权一个未知绑定。
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).validate_environment(
            bundle, config, queue.research.corpus
        )
    assert error.value.code == ErrorCode.UNSUPPORTED_SCHEMA

    # 使用当前后端可信 fixture 的绑定；不从录制环境反推效果或签名。
    binding = worker().resolver.signature

    class PureEffectfulTools(ToolRegistry):
        def effect(self, name):
            if name == "corpus_search":
                return EffectClass.NON_IDEMPOTENT_WRITE
            return super().effect(name)

        def signature(self, name):
            return binding if name == "corpus_search" else super().signature(name)

    before = counts()
    assert before["requests"] == len(before["entries"]) == 1
    branch = history.branch(repo)

    def forbidden(*args, **kwargs):
        pytest.fail("完整外部写 Recorded 不得调用模型、工具、Resolver、网络、进程或工作区")

    checked = []
    with monkeypatch.context() as offline:
        offline.setattr("agentgit.replay.engine.ToolRegistry", PureEffectfulTools)
        for entry in (
            "agentgit.adapters.tools.ToolRegistry.invoke",
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.fake_effect.FakeEffectAdapter.__init__",
            "agentgit.adapters.fake_effect.FakeEffectAdapter.invoke",
            "agentgit.adapters.fake_effect.FakeEffectAdapter.resolve",
            "agentgit.services.reconciliation.ReconciliationService.confirm",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
            "socket.create_connection",
            "socket.socket",
            "subprocess.Popen",
        ):
            offline.setattr(entry, forbidden)
        replayed = RecordedReplayer(history.objects.get).run(
            bundle, config, queue.research.corpus, on_checkpoint=checked.append
        )
    assert len(checked) == len(bundle.checkpoints) - 1
    assert checked[-1].snapshot == history.restore(repo, head)
    assert replayed.actual_model_calls == replayed.actual_tool_calls == 0
    assert replayed.state.usage.reserved_tokens == 0
    assert counts() == before and history.branch(repo) == branch
