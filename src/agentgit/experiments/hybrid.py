"""无外部调用的 Hybrid 选择；精确命中才可消费历史响应。"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import ReadArguments, SearchArguments
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.hybrid import FrozenHybridPolicy, HybridCall
from agentgit.core.models import (
    CallOccurrence,
    Contract,
    Digest,
    EffectClass,
    Identifier,
    parse_contract,
)
from agentgit.core.research import CallTrace, Source
from agentgit.replay.recorded import Recording


class ObservationProvenance(Contract):
    kind: Literal["recorded", "synthetic", "fixture"]
    selected_call_id: Identifier
    matched: bool
    response_ref: Digest
    intervention_id: Identifier | None = None
    test_only: Literal[True] = True


@dataclass(frozen=True)
class PreparedObservation:
    payload: bytes
    error: AgentGitError | None
    provenance: ObservationProvenance


def slot(item: CallOccurrence | CallTrace) -> tuple[str, int, int]:
    return item.logical_step_id, item.call_index, item.attempt


class HybridSelector:
    def __init__(
        self,
        policy: FrozenHybridPolicy,
        corpus: Corpus,
        read: Callable[[str, ObjectType], bytes],
    ):
        self.policy = parse_contract(encode(policy.model_dump(mode="json")), FrozenHybridPolicy)
        captured = Corpus(tuple(corpus.sources.values()), evaluation_time=corpus.evaluation_time)
        if captured.digest() != self.policy.corpus_digest:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Hybrid 模拟器与冻结语料不符")
        self.corpus, self.read = captured, read
        self.calls = {slot(call.intent.occurrence): call for call in self.policy.calls}

    def resolve(
        self, occurrence: CallOccurrence, effect: EffectClass, request: bytes, max_bytes: int
    ) -> PreparedObservation | None:
        # 模型决策始终重新生成；不能用旧模型输出证明干预后的结果。
        if occurrence.provider.startswith("llm:"):
            return None
        selected = self.calls.get(slot(occurrence))
        if selected is None:
            return None
        original = self._record(selected)
        matched = (
            occurrence == selected.intent.occurrence
            and effect == selected.intent.effect
            and request == original.request
            and max_bytes == selected.intent.max_response_bytes
        )
        kind: Literal["recorded", "synthetic", "fixture"]
        if object_digest(ObjectType.BLOB, request) != occurrence.request_digest:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Hybrid 当前请求与 occurrence 摘要不符")
        if not matched:
            if self.policy.miss_policy == "stop":
                raise AgentGitError(
                    ErrorCode.REPLAY_MISMATCH, "Hybrid 选定调用不匹配，冻结策略要求停止"
                )
            if (
                effect != EffectClass.READ_ONLY
                or occurrence.provider != selected.intent.occurrence.provider
                or occurrence.signature_digest != selected.intent.occurrence.signature_digest
            ):
                raise AgentGitError(
                    ErrorCode.REPLAY_MISMATCH, "受控模拟器不接受效果、工具或签名变更"
                )
            payload = self._simulate(occurrence.provider, request)
            error, kind, intervention = None, "fixture", None
        elif selected.replacement_response_ref is not None:
            payload = self.read(selected.replacement_response_ref, ObjectType.BLOB)
            validate_observation(occurrence.provider, request, payload)
            error, kind, intervention = None, "synthetic", selected.intervention_id
        else:
            outcome = original.outcome()
            payload, error, kind, intervention = outcome.payload, outcome.error, "recorded", None
        if len(payload) > max_bytes:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Hybrid 完整观测超过当前响应预算")
        return PreparedObservation(
            payload,
            error,
            ObservationProvenance(
                kind=kind,
                selected_call_id=selected.source_call_id,
                matched=matched,
                response_ref=object_digest(ObjectType.BLOB, payload),
                intervention_id=intervention,
            ),
        )

    def _record(self, selected: HybridCall) -> Recording:
        intent = selected.intent
        record = Recording(
            intent,
            selected.saved,
            self.read(intent.request_ref, ObjectType.BLOB),
            self.read(selected.saved.response_ref, ObjectType.BLOB),
            selected.source_call_id,
        )
        record.outcome()
        return record

    def _simulate(self, provider: str, request: bytes) -> bytes:
        if provider == "tool:corpus_search/v1":
            search = parse_contract(request, SearchArguments)
            if not set(search.frameworks) <= {
                source.framework for source in self.corpus.sources.values()
            }:
                raise AgentGitError(ErrorCode.VALIDATION, "模拟搜索只支持冻结语料框架")
            value = {"schema_version": 1, "source_ids": list(self.corpus.search(search.frameworks))}
        elif provider == "tool:source_read/v1":
            read = parse_contract(request, ReadArguments)
            value = {
                "schema_version": 1,
                "source": self.corpus.read(read.source_id).model_dump(mode="json"),
            }
        else:
            raise AgentGitError(ErrorCode.FORBIDDEN, "未声明的 Hybrid 模拟工具")
        return encode(value)

    def require_reached(self, messages: tuple[CallTrace, ...]) -> None:
        observed = {(slot(trace), trace.provider) for trace in messages}
        if any(
            (slot(call.intent.occurrence), call.intent.occurrence.provider) not in observed
            for call in self.policy.calls
        ):
            raise AgentGitError(
                ErrorCode.RECORDING_MISSING, "Hybrid 仍有未到达的选定依赖，不能宣告完成"
            )


class SearchObservation(Contract):
    source_ids: tuple[str, ...]


class SourceObservation(Contract):
    source: Source


def validate_observation(provider: str, request: bytes, response: bytes) -> None:
    if provider == "tool:corpus_search/v1":
        parse_contract(request, SearchArguments)
        search = parse_contract(response, SearchObservation)
        if len(set(search.source_ids)) != len(search.source_ids) or len(search.source_ids) > 1000:
            raise AgentGitError(ErrorCode.VALIDATION, "替换搜索观测的来源重复或超限")
    elif provider == "tool:source_read/v1":
        read = parse_contract(request, ReadArguments)
        source = parse_contract(response, SourceObservation).source
        if source.id != read.source_id:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "替换来源观测不能改变请求的来源身份")
        # 此处校验正文完整性；事实有效性仍由最终冻结验收器独立判断。
        if hashlib.sha256(source.body.encode()).hexdigest() != source.body_sha256:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "替换观测的正文摘要不符")
    else:
        raise AgentGitError(ErrorCode.FORBIDDEN, "只能替换受控只读工具观测")
