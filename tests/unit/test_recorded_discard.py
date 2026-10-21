"""丢弃录制的纯预算结算，拒绝输入漂移、重复结算及未知状态改写。"""

from dataclasses import replace

import pytest

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import ExecutionContext, StoppedExecution
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass, RunStatus, parse_contract
from agentgit.core.research import (
    AgentState,
    BudgetUsage,
    CallTrace,
    Plan,
    PlanNode,
    ResearchGoal,
    ToolDecision,
    evolve,
)
from agentgit.replay.recovery import CleanupProof, DiscardEvent, RecordedDiscard, rebuild_discard


@pytest.fixture
def stopped_record():
    request = {
        "schema_version": 1,
        "code_ref": "a" * 64,
        "input_manifest_ref": "b" * 64,
        "execution_policy_ref": "c" * 64,
    }
    raw = encode(request)
    occurrence = CallOccurrence(
        logical_step_id="code",
        call_index=0,
        attempt=1,
        provider="tool:code_exec/v1",
        signature_digest="d" * 64,
        request_digest=object_digest(ObjectType.BLOB, raw),
    )
    intent = CallIntent(
        occurrence=occurrence,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request_ref=occurrence.request_digest,
        checkpoint="e" * 64,
        max_response_bytes=8192,
    )
    certificate = StoppedExecution(
        occurrence=occurrence, input_manifest_ref="b" * 64, execution_policy_ref="c" * 64
    )
    before = AgentState(
        goal=ResearchGoal(text="比较三个 fixture 框架"),
        status=RunStatus.RUNNING,
        plan=Plan(nodes=(PlanNode(key="code", kind="code_exec"),)),
        usage=BudgetUsage(tool_calls=5),
        pending_tool=ToolDecision(node_key="code", tool="code_exec", arguments=request),
    )
    event = DiscardEvent(
        call_id="00000000-0000-0000-0000-000000000001",
        cleanup_evidence_ref="f" * 64,
        stopped_execution=certificate,
        charged_tool_calls=1,
    )
    record = RecordedDiscard("1" * 64, event, intent, raw, before)
    context = ExecutionContext(input_manifest_ref="b" * 64, execution_policy_ref="c" * 64, files=())
    return record, context


def settle(record, state, context):
    return rebuild_discard(
        record, state, context, signature="d" * 64, max_response_bytes=8192, max_attempts=2
    )


def unknown(record):
    occurrence, before = record.intent.occurrence, record.before
    trace = CallTrace(
        logical_step_id=occurrence.logical_step_id,
        call_index=0,
        attempt=1,
        provider=occurrence.provider,
        signature_digest=occurrence.signature_digest,
        request_ref=occurrence.request_digest,
        response_ref="2" * 64,
        error_code="outcome_unknown",
    )
    return evolve(
        before,
        status=RunStatus.OUTCOME_UNKNOWN,
        error_code="outcome_unknown",
        resume_status=RunStatus.RUNNING,
        usage=evolve(before.usage, tool_calls=6),
        attempts={"code": 1},
        messages=(trace,),
    )


def test_discard_from_known_input_charges_exactly_once_without_response(stopped_record):
    record, context = stopped_record
    after = settle(record, record.before, context)
    assert record.before.usage.tool_calls == 5 and after.usage.tool_calls == 6
    assert after.attempts == {"code": 1} and after.pending_tool == record.before.pending_tool
    assert after.stopped_executions == (record.event.stopped_execution,)
    assert after.messages == ()
    with pytest.raises(AgentGitError) as error:
        settle(record, after, context)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_unknown_budget_is_preserved_and_does_not_charge_again(stopped_record):
    record, context = stopped_record
    state = unknown(record)
    after = settle(record, state, context)
    assert after.usage.tool_calls == state.usage.tool_calls == 6
    assert after.messages == state.messages and after.status == RunStatus.RUNNING
    assert after.error_code is None and after.resume_status is None


@pytest.mark.parametrize(
    "tamper", ["usage", "attempt", "state", "trace", "input", "charge", "duplicate"]
)
def test_discard_rejects_state_or_evidence_drift(stopped_record, tamper):
    record, context = stopped_record
    state = unknown(record)
    if tamper == "usage":
        state = evolve(state, usage=evolve(state.usage, tool_calls=7))
    elif tamper == "attempt":
        state = evolve(state, attempts={"code": 2})
    elif tamper == "state":
        state = evolve(state, plan_version=7)
    elif tamper == "trace":
        state = evolve(state, messages=(evolve(state.messages[0], call_index=1),))
    elif tamper == "input":
        context = evolve(context, input_manifest_ref="3" * 64)
    elif tamper == "charge":
        record = replace(record, event=record.event.model_copy(update={"charged_tool_calls": True}))
    else:
        before = evolve(record.before, stopped_executions=(record.event.stopped_execution,))
        record = replace(record, before=before)
        state = before
    with pytest.raises(AgentGitError) as error:
        settle(record, state, context)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_cleanup_proof_rejects_integer_instead_of_boolean():
    with pytest.raises(AgentGitError) as error:
        parse_contract(
            encode(
                {
                    "schema_version": 1,
                    "container_name": "fixture",
                    "container_id": None,
                    "cleanup_confirmed": 1,
                    "previous_fencing_token": 1,
                    "observer_fencing_token": 2,
                }
            ),
            CleanupProof,
        )
    assert error.value.code == ErrorCode.VALIDATION
