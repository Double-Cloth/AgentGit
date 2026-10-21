"""仅消费已录制字节，不持有模型、工具、容器或网络客户端。"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from agentgit.core.canonical import ObjectType, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass
from agentgit.core.research import CallTrace, SavedCallOutcome, evolve
from agentgit.runtime.ports import CallOutcome


@dataclass(frozen=True)
class Recording:
    intent: CallIntent
    saved: SavedCallOutcome
    request: bytes
    response: bytes
    source_call_id: str
    reused: bool = False

    def outcome(self) -> CallOutcome:
        occurrence = self.intent.occurrence
        if (
            object_digest(ObjectType.BLOB, self.request) != self.intent.request_ref
            or self.intent.request_ref != occurrence.request_digest
            or object_digest(ObjectType.BLOB, self.response) != self.saved.response_ref
            or len(self.response) > self.intent.max_response_bytes
            or self.saved.response_bytes < len(self.response)
            or (self.saved.complete and self.saved.response_bytes != len(self.response))
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "录制摘要、字节长度或响应限制不符")
        error = self.saved.error
        if error is not None and error["code"] == ErrorCode.OUTCOME_UNKNOWN.value:
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "未知效果不能作为完整录制消费")
        failure = AgentGitError(ErrorCode(error["code"]), error["message"]) if error else None
        trace = CallTrace(
            logical_step_id=occurrence.logical_step_id,
            call_index=occurrence.call_index,
            attempt=occurrence.attempt,
            provider=occurrence.provider,
            signature_digest=occurrence.signature_digest,
            request_ref=occurrence.request_digest,
            response_ref=self.saved.response_ref,
            error_code=failure.code.value if failure else None,
        )
        return CallOutcome(self.response, trace, failure, self.saved.response_bytes)


@dataclass(frozen=True)
class UncertainRecording(Recording):
    """仅重现已核验的历史未知诊断，不把它当作确认结果。"""

    def outcome(self) -> CallOutcome:
        error = self.saved.error
        if self.reused or error is None or error["code"] != ErrorCode.OUTCOME_UNKNOWN.value:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知录制缺少明确诊断")
        known = Recording(
            self.intent,
            evolve(self.saved, error=None),
            self.request,
            self.response,
            self.source_call_id,
            self.reused,
        ).outcome()
        return replace(
            known,
            trace=evolve(known.trace, error_code=ErrorCode.OUTCOME_UNKNOWN.value),
            error=AgentGitError(ErrorCode.OUTCOME_UNKNOWN, error["message"]),
        )


class RecordedAdapter:
    def __init__(self, recordings: tuple[Recording, ...], checkpoint: Callable[[], str | None]):
        if (
            len(recordings) > 10000
            or sum(len(item.request) + len(item.response) for item in recordings) > 67108864
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "录制范围超过有界内存上限，请缩小起止范围")
        self.recordings, self.checkpoint = recordings, checkpoint
        self.position = 0

    def take(
        self,
        occurrence: CallOccurrence,
        effect: EffectClass,
        request: bytes,
        *,
        checkpoint: str | None,
        max_bytes: int,
    ) -> CallOutcome:
        if self.position >= len(self.recordings):
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "选定范围缺少下一次调用录制")
        record = self.recordings[self.position]
        if (
            occurrence != record.intent.occurrence
            or effect != record.intent.effect
            or request != record.request
            or checkpoint != record.intent.checkpoint
            or max_bytes != record.intent.max_response_bytes
        ):
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "录制 occurrence、请求、效果或检查点错配"
            )
        outcome = record.outcome()
        self.position += 1
        return outcome

    def call(
        self,
        *,
        step: str,
        attempt: int,
        provider: str,
        signature: str,
        effect: EffectClass,
        request: bytes,
        invoke: Callable[[], bytes],
        max_bytes: int,
    ) -> CallOutcome:
        # invoke 只为兼容 Runtime 边界；任何成功或失败路径都不调用它。
        return self.take(
            CallOccurrence(
                logical_step_id=step,
                call_index=0,
                attempt=attempt,
                provider=provider,
                signature_digest=signature,
                request_digest=object_digest(ObjectType.BLOB, request),
            ),
            effect,
            request,
            checkpoint=self.checkpoint(),
            max_bytes=max_bytes,
        )

    def recorded(
        self,
        *,
        step: str,
        attempt: int,
        provider: str,
        signature: str,
        effect: EffectClass,
        request: bytes,
        max_bytes: int,
    ) -> CallOutcome | None:
        # 这是新的 Recorded 运行，不使用崩溃恢复的未观察调用结算入口。
        return None

    def assert_consumed(self) -> None:
        if self.position != len(self.recordings):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "选定范围仍有未消费的录制调用")
