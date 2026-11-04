"""回放说明的有界契约及控制依据不能互相借用。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph_recorded import (
    LangGraphRecordedControl,
    LangGraphRecordedSpec,
    LangGraphRecordingBundle,
)
from agentgit.core.langgraph_runs import LangGraphForkSpec, LangGraphJobPayload, LangGraphRunState
from agentgit.core.models import RunStatus
from agentgit.services.langgraph_recordings import _controls, _parent_control, projection_digest


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


@pytest.fixture
def parent_control():
    running = LangGraphRunState(program_ref="a" * 64)
    paused = running.model_copy(update={"status": RunStatus.PAUSED, "replay_pause_pending": True})
    resumed = running.model_copy(update={"replay_control_index": 1})
    item = LangGraphRecordedControl(
        kind="requested",
        before_digest=projection_digest(running),
        paused_digest=projection_digest(paused),
        resumed_digest=projection_digest(resumed),
        pause_event_ref="b" * 64,
        pause_request_ref="c" * 64,
        resume_event_ref="d" * 64,
        resume_request_ref="e" * 64,
    )
    bundle = LangGraphRecordingBundle(
        source_run_id="00000000-0000-0000-0000-000000000001",
        from_commit="a" * 64,
        to_commit="b" * 64,
        start_commit="a" * 64,
        program_ref="a" * 64,
        source_payload_ref="c" * 64,
        from_state_ref="d" * 64,
        start_state_ref="d" * 64,
        to_state_ref="e" * 64,
        environment_ref="f" * 64,
        call_id="00000000-0000-0000-0000-000000000002",
        intent_ref="a" * 64,
        result_ref="b" * 64,
        needs_fetch=True,
        needs_fork_initialization=False,
        controls=(item, item),
    )
    parent = SimpleNamespace(bundle=bundle)
    ref = object_digest(ObjectType.BLOB, encode(bundle.model_dump(mode="json")))
    return parent, {"bundle_ref": ref, "index": 0}, running, paused, resumed


@pytest.mark.parametrize(
    "change",
    [
        {"bundle_ref": "0" * 64},
        {"bundle_ref": None},
        {"index": True},
        {"index": False},
        {"index": 0.0},
        {"index": "0"},
        {"index": -1},
        {"index": 2},
        {"extra": True},
    ],
)
def test_parent_control_requires_exact_bundle_and_integer_index(parent_control, change):
    parent, recorded, *_ = parent_control
    with pytest.raises(AgentGitError) as caught:
        _parent_control(parent, {**recorded, **change})
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH


def test_parent_control_rejects_non_object_and_missing_parent(parent_control):
    parent, recorded, *_ = parent_control
    for holder, value in [(None, recorded), (parent, []), (parent, "index"), (parent, {})]:
        with pytest.raises(AgentGitError):
            _parent_control(holder, value)
    assert _parent_control(parent, recorded) == parent.bundle.controls[0]


@pytest.mark.parametrize("change", [None, "other_pair", "pause_cursor", "resume_cursor"])
def test_control_pair_keeps_same_parent_index_and_durable_cursor(parent_control, change):
    parent, recorded, running, paused, resumed = parent_control
    if change == "pause_cursor":
        paused = paused.model_copy(update={"replay_control_index": 1})
    if change == "resume_cursor":
        resumed = resumed.model_copy(update={"replay_control_index": 0})
    pause_event = {"seq": 2, "type": "langgraph.control_applied", "payload_ref": "a" * 64}
    resume_event = {"seq": 3, "type": "langgraph.resumed", "payload_ref": "b" * 64}
    bodies = {
        2: {"recorded_control": recorded},
        3: {"recorded_control": {**recorded, "index": 1 if change == "other_pair" else 0}},
    }
    args = (
        ["start", "pause", "resume"],
        {"start": running, "pause": paused, "resume": resumed},
        {"start": [], "pause": [pause_event], "resume": [resume_event]},
        [pause_event, resume_event],
        bodies,
        parent,
    )
    if change is None:
        pairs = _controls(*args)
        assert (
            len(pairs) == 1
            and pairs[0][2].pause_request_ref == parent.bundle.controls[0].pause_request_ref
        )
    else:
        with pytest.raises(AgentGitError) as caught:
            _controls(*args)
        assert caught.value.code == ErrorCode.REPLAY_MISMATCH
