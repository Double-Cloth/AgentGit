"""从已验证的停止证明重算丢弃和旧尝试预算；不操作容器。"""

from dataclasses import dataclass
from typing import Literal

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import SANDBOX_PROVIDERS, ExecutionContext, StoppedExecution
from agentgit.core.models import CallIntent, Contract, Digest, EffectClass, Identifier, RunStatus
from agentgit.core.research import AgentState, evolve


class DiscardEvent(Contract):
    call_id: Identifier
    cleanup_evidence_ref: Digest
    stopped_execution: StoppedExecution
    charged_tool_calls: Literal[1]


class CleanupProof(Contract):
    container_name: str
    container_id: str | None
    cleanup_confirmed: bool
    previous_fencing_token: int
    observer_fencing_token: int


@dataclass(frozen=True)
class RecordedDiscard:
    source_commit: str
    event: DiscardEvent
    intent: CallIntent
    request: bytes
    before: AgentState


def rebuild_discard(
    record: RecordedDiscard,
    state: AgentState,
    context: ExecutionContext,
    *,
    signature: str,
    max_response_bytes: int,
    max_attempts: int,
) -> AgentState:
    before, intent, stopped = record.before, record.intent, record.event.stopped_execution
    occurrence, decision = stopped.occurrence, before.pending_tool
    if (
        decision is None
        or before.status != RunStatus.RUNNING
        or occurrence.provider not in SANDBOX_PROVIDERS
        or occurrence != intent.occurrence
        or intent.checkpoint is None
        or intent.effect != EffectClass.NON_IDEMPOTENT_WRITE
        or intent.max_response_bytes != max_response_bytes
        or occurrence.signature_digest != signature
        or occurrence.logical_step_id != decision.node_key
        or occurrence.provider != f"tool:{decision.tool}/v1"
        or occurrence.attempt != before.attempts.get(decision.node_key, 0) + 1
        or not 1 <= occurrence.attempt <= max_attempts
        or record.request != encode(decision.arguments)
        or object_digest(ObjectType.BLOB, record.request) != intent.request_ref
        or intent.request_ref != occurrence.request_digest
        or stopped.input_manifest_ref != context.input_manifest_ref
        or stopped.execution_policy_ref != context.execution_policy_ref
        or any(item.occurrence == occurrence for item in before.stopped_executions)
        or type(record.event.charged_tool_calls) is not int
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "录制丢弃与旧执行、输入、策略或预算不符")
    usage = evolve(before.usage, tool_calls=before.usage.tool_calls + 1)
    attempts = {**before.attempts, decision.node_key: occurrence.attempt}
    if state.status == RunStatus.OUTCOME_UNKNOWN:
        normalized = evolve(
            state,
            status=before.status,
            error_code=before.error_code,
            usage=before.usage,
            attempts=before.attempts,
            messages=before.messages,
            resume_status=before.resume_status,
        )
        extra = state.messages[len(before.messages) :]
        if (
            normalized != before
            or state.error_code != ErrorCode.OUTCOME_UNKNOWN.value
            or state.resume_status != before.status
            or state.usage != usage
            or state.attempts != attempts
            or state.messages[: len(before.messages)] != before.messages
            or len(extra) > 1
            or (
                extra
                and (
                    extra[0].logical_step_id != occurrence.logical_step_id
                    or extra[0].call_index != occurrence.call_index
                    or extra[0].attempt != occurrence.attempt
                    or extra[0].provider != occurrence.provider
                    or extra[0].signature_digest != occurrence.signature_digest
                    or extra[0].request_ref != occurrence.request_digest
                    or extra[0].error_code != ErrorCode.OUTCOME_UNKNOWN.value
                )
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知执行的录制状态或已占预算不符")
    elif state != before:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "丢弃不能重置或改写原调用前状态")
    return evolve(
        before,
        usage=usage,
        attempts=attempts,
        messages=state.messages,
        stopped_executions=(*before.stopped_executions, stopped),
    )
