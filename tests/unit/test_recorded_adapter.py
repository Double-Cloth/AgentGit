"""Recorded 必须按 occurrence 顺序消费；任意错配都不能调用真实回调。"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import ObjectType, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass
from agentgit.core.research import SavedCallOutcome
from agentgit.core.versions import RecordedRange
from agentgit.replay.recorded import RecordedAdapter, Recording


def recording(index=0, response=b"first", *, error=None, complete=True, response_bytes=None):
    request = b'{"same":"arguments"}'
    occurrence = CallOccurrence(
        logical_step_id="repeat",
        call_index=index,
        attempt=1,
        provider="tool:fixture/v1",
        signature_digest="f" * 64,
        request_digest=object_digest(ObjectType.BLOB, request),
    )
    intent = CallIntent(
        occurrence=occurrence,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request_ref=occurrence.request_digest,
        checkpoint="c" * 64,
        max_response_bytes=128,
    )
    saved = SavedCallOutcome(
        response_ref=object_digest(ObjectType.BLOB, response),
        response_bytes=len(response) if response_bytes is None else response_bytes,
        complete=complete,
        error=error,
    )
    return Recording(intent, saved, request, response, str(index))


def take(adapter, record, **changes):
    arguments = {
        "occurrence": record.intent.occurrence,
        "effect": record.intent.effect,
        "request": record.request,
        "checkpoint": record.intent.checkpoint,
        "max_bytes": record.intent.max_response_bytes,
    }
    return adapter.take(**{**arguments, **changes})


def test_same_arguments_with_distinct_occurrences_keep_distinct_responses():
    first, second = recording(), recording(1, b"second")
    adapter = RecordedAdapter((first, second), lambda: "c" * 64)
    with pytest.raises(AgentGitError) as error:
        take(adapter, second)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH and adapter.position == 0
    assert take(adapter, first).payload == b"first"
    assert take(adapter, second).payload == b"second"
    adapter.assert_consumed()


@pytest.mark.parametrize(
    "field,value",
    [
        ("logical_step_id", "other"),
        ("attempt", 2),
        ("call_index", 1),
        ("provider", "tool:other/v1"),
        ("signature_digest", "b" * 64),
    ],
)
def test_mismatched_identity_never_consumes_the_next_record(field, value):
    record = recording()
    adapter = RecordedAdapter((record,), lambda: "c" * 64)
    with pytest.raises(AgentGitError) as error:
        take(adapter, record, occurrence=record.intent.occurrence.model_copy(update={field: value}))
    assert error.value.code == ErrorCode.REPLAY_MISMATCH and adapter.position == 0


@pytest.mark.parametrize(
    "change",
    [
        {"request": b"changed"},
        {"checkpoint": "d" * 64},
        {"max_bytes": 127},
        {"effect": EffectClass.READ_ONLY},
    ],
)
def test_mismatched_request_policy_or_checkpoint_stops(change):
    record = recording()
    adapter = RecordedAdapter((record,), lambda: "c" * 64)
    with pytest.raises(AgentGitError) as error:
        take(adapter, record, **change)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_no_network_or_process_and_no_callback_even_for_external_write(monkeypatch):
    record = recording()
    sent = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded 不能发起网络或进程")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    adapter = RecordedAdapter((record,), lambda: record.intent.checkpoint)
    outcome = adapter.call(
        step="repeat",
        attempt=1,
        provider="tool:fixture/v1",
        signature="f" * 64,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request=record.request,
        invoke=lambda: sent.append("external-write"),
        max_bytes=128,
    )
    assert outcome.payload == record.response and sent == []
    with pytest.raises(AgentGitError) as error:
        adapter.call(
            step="repeat",
            attempt=1,
            provider="tool:fixture/v1",
            signature="f" * 64,
            effect=EffectClass.NON_IDEMPOTENT_WRITE,
            request=record.request,
            invoke=forbidden,
            max_bytes=128,
        )
    assert error.value.code == ErrorCode.RECORDING_MISSING


def test_corrupt_bytes_and_unknown_results_cannot_be_consumed():
    original = recording()
    corrupt = replace(original, response=b"changed")
    adapter = RecordedAdapter((corrupt,), lambda: "c" * 64)
    with pytest.raises(AgentGitError) as error:
        take(adapter, corrupt)
    assert error.value.code == ErrorCode.OBJECT_CORRUPT and adapter.position == 0
    unknown = recording(error={"code": "outcome_unknown", "message": "unknown"})
    with pytest.raises(AgentGitError) as error:
        take(RecordedAdapter((unknown,), lambda: "c" * 64), unknown)
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN


def test_truncated_record_retains_original_usage_and_known_error():
    record = recording(
        response=b"cut",
        complete=False,
        response_bytes=500,
        error={"code": "output_limit", "message": "truncated"},
    )
    result = take(RecordedAdapter((record,), lambda: "c" * 64), record)
    assert result.output_bytes == 500 and result.error.code == ErrorCode.OUTPUT_LIMIT


def test_unused_records_and_recorded_overrides_are_explicitly_rejected():
    with pytest.raises(AgentGitError):
        RecordedAdapter((recording(),), lambda: "c" * 64).assert_consumed()
    RecordedAdapter((), lambda: "c" * 64).assert_consumed()
    with pytest.raises(ValidationError):
        RecordedRange(
            source_run_id="00000000-0000-0000-0000-000000000000",
            from_commit="a" * 64,
            until_commit="b" * 64,
            overrides={},
        )
