"""真实调用账本、事件顺序、环境与对象完整性约束。"""

import pytest
from test_approvals import action as action
from test_approvals import decide
from test_approvals import effect_server as effect_server

from agentgit.core.canonical import ObjectType
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass, EnvironmentManifest
from agentgit.core.versions import RecordedRange
from agentgit.replay.recorded import RecordedAdapter
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.history import EventInput
from agentgit.services.recordings import RecordingReader

pytestmark = pytest.mark.integration


@pytest.fixture
def recorded_source(history):
    repo = history.create_repository("录制来源")
    run = history.create_run(repo)
    environment = EnvironmentManifest(
        runtime_version="fixture",
        python_version="3.12",
        code_revision="fixture",
        dependency_lock_digest="a" * 64,
        model_signature="no-model",
        prompt_digest="b" * 64,
        tool_signatures=("c" * 64,),
        input_ref=history.register_input(repo, b"source"),
        evaluation_time="2026-10-21T00:00:00Z",
        random_seed=0,
    )
    head, version, messages, commits = None, 0, [], []

    def save():
        nonlocal head, version
        result = history.commit(
            repo,
            run,
            expected_head=head,
            expected_version=version,
            state={"messages": list(messages), "count": len(messages)},
            environment=environment,
            events=(EventInput("fixture.observed", "state", {}),),
        )
        head, version = result.commit_id, result.branch_version
        commits.append(head)

    save()
    policy = ToolPolicy(
        rules=(
            ToolRule(
                provider="tool:repeated/v1",
                signature_digest="c" * 64,
                effect=EffectClass.READ_ONLY,
                scope="local",
                preauthorized=True,
            ),
        )
    )
    gateway = RecordedCallGateway(
        history,
        repo,
        run,
        lambda: (head, version),
        authorization=ApprovalService(history, policy, actor="local-operator"),
    )
    for attempt, response in ((1, b"first"), (2, b"second")):
        outcome = gateway.call(
            step="repeat",
            attempt=attempt,
            provider="tool:repeated/v1",
            signature="c" * 64,
            effect=EffectClass.READ_ONLY,
            request=b"same arguments",
            invoke=lambda response=response: response,
            max_bytes=128,
        )
        messages.append(outcome.trace.model_dump(mode="json"))
        save()
    return repo, run, commits, environment


def test_reader_preserves_event_occurrence_and_adapter_can_run_offline(
    history, recorded_source, monkeypatch
):
    repo, run, commits, environment = recorded_source
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(
            source_run_id=str(run),
            from_commit=commits[0],
            until_commit=commits[2],
        ),
        expected_environment=environment,
    )
    assert [item.intent.occurrence.attempt for item in bundle.recordings] == [1, 2]
    assert len(bundle.checkpoints) == 3

    def no_network(*args, **kwargs):
        raise AssertionError("离线 Adapter 不能访问网络")

    monkeypatch.setattr("socket.socket", no_network)
    adapter = RecordedAdapter(bundle.recordings, lambda: None)
    outputs = [
        adapter.take(
            item.intent.occurrence,
            item.intent.effect,
            item.request,
            checkpoint=item.intent.checkpoint,
            max_bytes=item.intent.max_response_bytes,
        ).payload
        for item in bundle.recordings
    ]
    assert outputs == [b"first", b"second"]
    adapter.assert_consumed()


def test_reader_zero_range_validates_state_without_claiming_execution(history, recorded_source):
    repo, run, commits, environment = recorded_source
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(
            source_run_id=str(run),
            from_commit=commits[1],
            until_commit=commits[1],
        ),
        expected_environment=environment,
    )
    assert len(bundle.checkpoints) == 1 and bundle.recordings == ()


@pytest.mark.parametrize("kind", [ObjectType.BLOB, ObjectType.COMMIT])
def test_reader_missing_object_fails_without_fallback(history, recorded_source, kind):
    repo, run, commits, environment = recorded_source
    request = RecordedRange(source_run_id=str(run), from_commit=commits[0], until_commit=commits[2])
    bundle = RecordingReader(history).load(repo, request, expected_environment=environment)
    reference = bundle.recordings[0].saved.response_ref if kind == ObjectType.BLOB else commits[1]
    path = history.objects.root / reference[:2] / reference[2:]
    assert path.resolve().is_relative_to(history.objects.root.resolve())
    raw = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(AgentGitError) as error:
            RecordingReader(history).load(repo, request, expected_environment=environment)
        assert error.value.code == ErrorCode.RECORDING_MISSING
    finally:
        history.objects.put(kind, raw)


@pytest.mark.parametrize(
    ("setting", "limit"), [("MAX_RECORDED_BYTES", 128), ("MAX_RECORDED_EVENTS", 1)]
)
def test_reader_bounds_cumulative_state_and_event_memory(
    history, recorded_source, monkeypatch, setting, limit
):
    repo, run, commits, environment = recorded_source
    monkeypatch.setattr("agentgit.services.recordings." + setting, limit)
    with pytest.raises(AgentGitError) as error:
        RecordingReader(history).load(
            repo,
            RecordedRange(source_run_id=str(run), from_commit=commits[0], until_commit=commits[2]),
            expected_environment=environment,
        )
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


def test_reader_environment_mismatch_and_cross_repository_are_not_accepted(
    history, recorded_source
):
    repo, run, commits, environment = recorded_source
    request = RecordedRange(source_run_id=str(run), from_commit=commits[0], until_commit=commits[2])
    with pytest.raises(AgentGitError) as error:
        RecordingReader(history).load(
            repo,
            request,
            expected_environment=environment.model_copy(update={"runtime_version": "different"}),
        )
    assert error.value.code == ErrorCode.UNSUPPORTED_SCHEMA
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError) as error:
        RecordingReader(history).load(other, request, expected_environment=environment)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_fake_external_write_record_is_consumed_without_touching_server(action, monkeypatch):
    history, repo, run, _, gateway, options, adapter, counts, _, original_env = action
    environment = original_env.model_copy(update={"tool_signatures": (adapter.signature,)})
    branch = history.branch(repo)
    before = history.commit(
        repo,
        run,
        expected_head=branch["head_commit_id"],
        expected_version=branch["version"],
        state={"fixture": "before-recorded-effect"},
        environment=environment,
        events=(EventInput("fixture.environment", "freeze", {}),),
    )
    decide(action)
    assert gateway.call(**options).error is None
    assert counts()["requests"] == 1
    branch = history.branch(repo)
    after = history.commit(
        repo,
        run,
        expected_head=branch["head_commit_id"],
        expected_version=branch["version"],
        state={"fixture": "after-recorded-effect"},
        environment=environment,
        events=(EventInput("fixture.observed", "done", {}),),
    )

    def no_external(*args, **kwargs):
        pytest.fail("Recorded 不能再次调用外部服务")

    monkeypatch.setattr(adapter, "invoke", no_external)
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(
            source_run_id=str(run),
            from_commit=before.commit_id,
            until_commit=after.commit_id,
        ),
        expected_environment=environment,
    )
    assert len(bundle.recordings) == 1
    record = bundle.recordings[0]
    replay = RecordedAdapter(bundle.recordings, lambda: before.commit_id)
    assert (
        replay.take(
            record.intent.occurrence,
            record.intent.effect,
            record.request,
            checkpoint=before.commit_id,
            max_bytes=record.intent.max_response_bytes,
        ).error
        is None
    )
    replay.assert_consumed()
    assert counts()["requests"] == 1
