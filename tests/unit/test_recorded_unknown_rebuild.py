"""未知封存保留已经观察的轨迹，拒绝重复计量和意图漂移。"""

from dataclasses import replace

import pytest
from test_recorded_discard import stopped_record as stopped_record
from test_recorded_discard import unknown

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass, RunStatus
from agentgit.core.research import evolve
from agentgit.replay.unknown import RecordedUnknownStop, rebuild_unknown


def seal(record, state):
    return rebuild_unknown(
        RecordedUnknownStop(
            record.source_commit, record.event.call_id, record.intent, record.request, record.before
        ),
        state,
        expected_request=record.request,
        provider="tool:code_exec/v1",
        signature="d" * 64,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        max_response_bytes=8192,
    )


def test_new_unknown_seal_counts_original_tool_once_without_inventing_response(stopped_record):
    record, _ = stopped_record
    after = seal(record, record.before)
    assert after.status == RunStatus.OUTCOME_UNKNOWN and after.resume_status == RunStatus.RUNNING
    assert after.usage.tool_calls == 6 and after.attempts == {"code": 1}
    assert after.messages == record.before.messages == ()
    assert seal(record, after) == after


def test_existing_unknown_trace_is_retained_without_another_charge(stopped_record):
    record, _ = stopped_record
    state = unknown(record)
    assert seal(record, state) == state
    assert state.usage.tool_calls == 6 and len(state.messages) == 1


@pytest.mark.parametrize(
    "tamper", ["budget", "attempt", "trace", "state", "effect", "limit", "call_index"]
)
def test_unknown_seal_rejects_already_charged_state_or_intent_drift(stopped_record, tamper):
    record, _ = stopped_record
    state = unknown(record)
    if tamper == "budget":
        state = evolve(state, usage=evolve(state.usage, tool_calls=7))
    elif tamper == "attempt":
        state = evolve(state, attempts={"code": 2})
    elif tamper == "trace":
        state = evolve(state, messages=(evolve(state.messages[0], signature_digest="0" * 64),))
    elif tamper == "state":
        state = evolve(state, completed=("code",))
    elif tamper == "effect":
        record = replace(record, intent=evolve(record.intent, effect=EffectClass.READ_ONLY))
    elif tamper == "limit":
        record = replace(record, intent=evolve(record.intent, max_response_bytes=8191))
    else:
        record = replace(
            record,
            intent=evolve(record.intent, occurrence=evolve(record.intent.occurrence, call_index=1)),
        )
    with pytest.raises(AgentGitError) as error:
        seal(record, state)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
