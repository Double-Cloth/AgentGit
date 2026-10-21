"""根据原调用意图重算接管封存，不发送或猜测调用结果。"""

from dataclasses import dataclass

from agentgit.core.canonical import ObjectType, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, EffectClass, RunStatus
from agentgit.core.research import AgentState, evolve


@dataclass(frozen=True)
class RecordedUnknownStop:
    source_commit: str
    call_id: str
    intent: CallIntent
    request: bytes
    before: AgentState


def rebuild_unknown(
    record: RecordedUnknownStop,
    state: AgentState,
    *,
    expected_request: bytes,
    provider: str,
    signature: str,
    effect: EffectClass,
    max_response_bytes: int,
) -> AgentState:
    before, intent = record.before, record.intent
    occurrence = intent.occurrence
    model = provider.startswith("llm:")
    decision = before.pending_tool
    if (
        before.status not in (RunStatus.PLANNING, RunStatus.RUNNING)
        or before.usage.reserved_tokens != 0
        or intent.checkpoint is None
        or intent.max_response_bytes != max_response_bytes
        or intent.effect != effect
        or occurrence.call_index != 0
        or occurrence.provider != provider
        or occurrence.signature_digest != signature
        or intent.request_ref != occurrence.request_digest
        or object_digest(ObjectType.BLOB, record.request) != intent.request_ref
        or record.request != expected_request
        or (
            model
            and (
                decision is not None
                or occurrence.logical_step_id != f"model-{before.usage.steps + 1}"
                or occurrence.attempt != 1
                or effect != EffectClass.READ_ONLY
            )
        )
        or (
            not model
            and (
                decision is None
                or before.status != RunStatus.RUNNING
                or provider != f"tool:{decision.tool}/v1"
                or occurrence.logical_step_id != decision.node_key
                or occurrence.attempt != before.attempts.get(decision.node_key, 0) + 1
            )
        )
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知封存与原调用请求、身份或预算不符")
    usage = evolve(
        before.usage,
        steps=before.usage.steps + int(model),
        model_calls=before.usage.model_calls + int(model),
        tool_calls=before.usage.tool_calls + int(not model),
        reserved_tokens=len(record.request) + max_response_bytes if model else 0,
    )
    attempts = (
        before.attempts
        if model
        else {**before.attempts, occurrence.logical_step_id: occurrence.attempt}
    )
    rebuilt = evolve(
        before,
        status=RunStatus.OUTCOME_UNKNOWN,
        resume_status=before.status,
        error_code=ErrorCode.OUTCOME_UNKNOWN.value,
        usage=usage,
        attempts=attempts,
    )
    if state.status == RunStatus.OUTCOME_UNKNOWN:
        # 原 Runtime 已观察诊断时仅保留原轨迹，不能重复计量。
        extra = state.messages[len(before.messages) :]
        if (
            evolve(state, messages=before.messages) != rebuilt
            or state.messages[: len(before.messages)] != before.messages
            or len(extra) > 1
            or (
                extra
                and (
                    extra[0].error_code != ErrorCode.OUTCOME_UNKNOWN.value
                    or any(
                        getattr(extra[0], key) != getattr(occurrence, key)
                        for key in (
                            "logical_step_id",
                            "call_index",
                            "attempt",
                            "provider",
                            "signature_digest",
                        )
                    )
                    or extra[0].request_ref != intent.request_ref
                )
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "已封存未知状态的轨迹或计量不符")
        return state
    if state != before:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知封存不能改写原调用前状态")
    return rebuilt
