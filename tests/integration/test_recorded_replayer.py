"""原 Runtime 逐检查点重算，不能用原快照冒充执行。"""

from dataclasses import replace
from unittest.mock import patch

import pytest

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus
from agentgit.core.research import AgentConfig, BudgetLimits, ResearchGoal, evolve
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RecordedReplayer
from agentgit.services.recordings import RecordingReader
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


@pytest.fixture
def replay_source(history, tmp_path):
    service = ResearchService(history, tmp_path / "recorded-source")
    repository = history.create_repository("真正的状态重算来源")
    config = service.create_config(repository)
    task = service.create_task(repository, config, ResearchGoal(text="比较三个 fixture 框架"))
    session = service.start(repository, task)
    final = session.execute()
    commits = tuple(
        item["commit_id"] for item in reversed(VersionService(history).log(repository)["items"])
    )

    def load(first=0, last=-1):
        return RecordingReader(history).load(
            repository,
            RecordedRange(
                source_run_id=str(session.run_id),
                from_commit=commits[first],
                until_commit=commits[last],
            ),
            expected_environment=session.environment,
        )

    return session, final, commits, load


def test_recorded_runtime_recomputes_every_hash_without_network_tools_or_host_workspace(
    history, replay_source, monkeypatch
):
    session, final, commits, load = replay_source
    bundle = load()
    original = history.branch(session.repository_id)
    source_files = session.workspace.contents()

    def forbidden(*args, **kwargs):
        pytest.fail("Recorded 不能构造工作区或调用真实模型、工具、网络、容器")

    monkeypatch.setattr("agentgit.adapters.mock.MockResearchModel.invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    monkeypatch.setattr(ArtifactWorkspace, "__init__", forbidden)
    monkeypatch.setattr("agentgit.adapters.execution.engine.DockerEngine.local", forbidden)
    monkeypatch.setattr(
        "agentgit.adapters.execution.container.SandboxContainer.__init__", forbidden
    )
    observed = []
    with patch("socket.socket", forbidden), patch("socket.create_connection", forbidden):
        result = RecordedReplayer(history.objects.get).run(
            bundle, session.config, session.service.corpus, on_checkpoint=observed.append
        )
    assert result.state == final
    assert result.consumed_calls == 20
    assert result.actual_model_calls == result.actual_tool_calls == 0
    assert result.restored_from == commits[0]
    assert tuple(item.source_commit for item in result.checkpoints) == commits[1:]
    for rebuilt, (source, expected) in zip(result.checkpoints, bundle.checkpoints[1:], strict=True):
        assert rebuilt.source_commit == source and rebuilt.snapshot == expected
        assert rebuilt.snapshot is not expected
        assert rebuilt.state_ref == object_digest(
            ObjectType.STATE, encode(expected.model_dump(mode="json"))
        )
    assert observed == list(result.checkpoints)
    assert history.branch(session.repository_id) == original
    assert session.workspace.contents() == source_files


@pytest.mark.parametrize(("first", "last"), [(1, 4), (8, 13), (0, 0), (20, 20)])
def test_recorded_range_stops_exactly_at_nonterminal_or_zero_range(
    history, replay_source, first, last
):
    session, _, commits, load = replay_source
    bundle = load(first, last)
    result = RecordedReplayer(history.objects.get).run(
        bundle, session.config, session.service.corpus
    )
    assert len(result.checkpoints) == last - first
    assert result.state.model_dump(mode="json") == bundle.checkpoints[-1][1].data
    assert result.restored_from == commits[first]


@pytest.mark.parametrize("tamper", ["initial", "witness", "missing_call", "config", "response"])
def test_recorded_runtime_rejects_changed_state_missing_call_config_or_response(
    history, replay_source, tamper
):
    session, _, _, load = replay_source
    bundle, config = load(), session.config
    if tamper in ("initial", "witness"):
        index = 0 if tamper == "initial" else 2
        commit_id, snapshot = bundle.checkpoints[index]
        altered = snapshot.model_copy(update={"data": {**snapshot.data, "plan_version": 99}})
        bundle = replace(
            bundle,
            checkpoints=(
                *bundle.checkpoints[:index],
                (commit_id, altered),
                *bundle.checkpoints[index + 1 :],
            ),
        )
    elif tamper == "missing_call":
        bundle = replace(bundle, recordings=bundle.recordings[:-1])
    elif tamper == "config":
        config = evolve(config, prompt="篡改 Prompt")
    else:
        first = bundle.recordings[0]
        bundle = replace(
            bundle, recordings=(replace(first, response=b"changed"), *bundle.recordings[1:])
        )
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).run(bundle, config, session.service.corpus)
    assert (
        error.value.code
        == {
            "initial": ErrorCode.REPLAY_MISMATCH,
            "witness": ErrorCode.REPLAY_MISMATCH,
            "missing_call": ErrorCode.RECORDING_MISSING,
            "config": ErrorCode.UNSUPPORTED_SCHEMA,
            "response": ErrorCode.OBJECT_CORRUPT,
        }[tamper]
    )


def test_recorded_environment_change_is_rejected_before_consuming_calls(history, replay_source):
    session, _, _, load = replay_source
    bundle = load()
    incompatible = replace(
        bundle, environment=bundle.environment.model_copy(update={"code_revision": "different"})
    )
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).run(incompatible, AgentConfig(), Corpus.bundled())
    assert error.value.code == ErrorCode.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "scenario", ["pause_resume", "cancel", "deadline", "wall", "steps", "tools"]
)
def test_recorded_rebuilds_control_and_budget_boundaries(history, tmp_path, scenario):
    from agentgit.adapters.mock import MockResearchModel

    limits = BudgetLimits(
        **(
            {"max_steps": 1}
            if scenario == "steps"
            else {"max_tool_calls": 0}
            if scenario == "tools"
            else {}
        )
    )
    config = AgentConfig(limits=limits)
    service = ResearchService(history, tmp_path / "boundary-source")
    repository = history.create_repository("受控时间与控制边界")
    config_id = service.create_config(repository, config)
    task = service.create_task(repository, config_id, ResearchGoal(text="比较三个 fixture 框架"))
    session = service.start(repository, task)
    fired = False

    def control():
        nonlocal fired
        if fired or session.state.pending_tool is None:
            return None
        if scenario in ("pause_resume", "cancel", "deadline"):
            fired = True
            return {"pause_resume": "pause", "cancel": "cancel", "deadline": "budget_exhausted"}[
                scenario
            ]
        return None

    logical_time = 0

    class SlowModel(MockResearchModel):
        def invoke(self, request):
            nonlocal logical_time
            response = super().invoke(request)
            logical_time = limits.max_wall_ms
            return response

    final = session.execute(
        SlowModel() if scenario == "wall" else MockResearchModel(),
        control=control,
        clock_ms=lambda: logical_time,
    )
    if scenario == "pause_resume":
        assert final.status == RunStatus.PAUSED
        resumed = False

        def resume():
            nonlocal resumed
            if not resumed:
                resumed = True
                return "resume"
            return None

        final = session.execute(control=resume)
        assert final.status == RunStatus.COMPLETED
    commits = tuple(
        item["commit_id"] for item in reversed(VersionService(history).log(repository)["items"])
    )
    bundle = RecordingReader(history).load(
        repository,
        RecordedRange(
            source_run_id=str(session.run_id), from_commit=commits[0], until_commit=commits[-1]
        ),
        expected_environment=session.environment,
    )
    result = RecordedReplayer(history.objects.get).run(bundle, config, service.corpus)
    assert result.state == final and len(result.checkpoints) == len(commits) - 1
    assert result.actual_model_calls == result.actual_tool_calls == 0
