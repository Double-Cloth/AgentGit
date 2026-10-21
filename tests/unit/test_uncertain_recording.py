"""未知诊断具有独立类型，不能冒充完整结果或缓存复用。"""

from dataclasses import replace

import pytest
from test_recorded_adapter import recording

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import evolve
from agentgit.replay.recorded import Recording, UncertainRecording


def uncertain():
    known = recording(response=b"")
    return UncertainRecording(
        known.intent,
        evolve(known.saved, error={"code": "outcome_unknown", "message": "回执丢失"}),
        known.request,
        known.response,
        known.source_call_id,
    )


def test_explicit_unknown_type_preserves_diagnostic_and_default_type_stays_blocked():
    value = uncertain()
    outcome = value.outcome()
    assert outcome.error.code == ErrorCode.OUTCOME_UNKNOWN
    assert outcome.payload == b"" and outcome.output_bytes == 0
    assert outcome.trace.error_code == "outcome_unknown"
    with pytest.raises(AgentGitError) as error:
        Recording(
            value.intent, value.saved, value.request, value.response, value.source_call_id
        ).outcome()
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN


@pytest.mark.parametrize("change", ["request", "response", "reused", "known"])
def test_unknown_diagnostic_rejects_corruption_reuse_or_success_disguise(change):
    value = uncertain()
    if change == "known":
        value = replace(value, saved=evolve(value.saved, error=None))
    else:
        value = replace(value, **{change: True if change == "reused" else b"changed"})
    with pytest.raises(AgentGitError) as error:
        value.outcome()
    assert error.value.code in (ErrorCode.OBJECT_CORRUPT, ErrorCode.REPLAY_MISMATCH)
