"""回放说明的有界契约及控制依据不能互相借用。"""

import pytest
from pydantic import ValidationError

from agentgit.core.langgraph_recorded import LangGraphRecordedControl, LangGraphRecordedSpec
from agentgit.core.langgraph_runs import LangGraphForkSpec, LangGraphJobPayload, LangGraphRunState


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "initial"},
        {"before_digest": None},
        {"pause_request_ref": None},
        {"kind": "execute"},
        {"resume_event_ref": "not-a-digest"},
    ],
)
def test_recorded_control_requires_complete_typed_basis(change):
    fields = dict(
        kind="requested",
        before_digest="a" * 64,
        paused_digest="b" * 64,
        resumed_digest="c" * 64,
        pause_event_ref="d" * 64,
        pause_request_ref="e" * 64,
        resume_event_ref="f" * 64,
        resume_request_ref="a" * 64,
    )
    with pytest.raises(ValidationError):
        LangGraphRecordedControl(**{**fields, **change})


def test_fork_and_recorded_initializers_are_exclusive():
    source = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValidationError):
        LangGraphJobPayload(
            program_ref="a" * 64,
            actor="operator",
            code_revision="source",
            dependency_lock_digest="b" * 64,
            max_wall_ms=1000,
            fork=LangGraphForkSpec(
                source_run_id=source,
                from_commit="a" * 64,
                source_program_ref="b" * 64,
                native_ref="c" * 64,
            ),
            replay=LangGraphRecordedSpec(
                source_run_id=source, from_commit="a" * 64, to_commit="b" * 64, bundle_ref="c" * 64
            ),
        )


def test_recorded_operational_cursors_do_not_change_logical_projection():
    before = LangGraphRunState(program_ref="a" * 64)
    after = before.model_copy(update={"replay_control_index": 1, "replay_call_consumed": True})
    assert before.logical_projection() == after.logical_projection()
