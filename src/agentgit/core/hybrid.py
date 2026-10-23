"""Hybrid 的显式调用选择与冻结响应；模型录制不进入选择集合。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import CallIntent, Contract, Digest, EffectClass, Identifier
from agentgit.core.research import SavedCallOutcome


class HybridPolicy(Contract):
    until_commit: Digest
    calls: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=100)]
    miss_policy: Literal["stop", "fixture"] = "stop"

    @model_validator(mode="after")
    def unique_selection(self) -> "HybridPolicy":
        if len(set(self.calls)) != len(self.calls):
            raise ValueError("Hybrid 不能重复选择同一调用")
        return self


class HybridCall(Contract):
    source_call_id: Identifier
    intent: CallIntent
    saved: SavedCallOutcome
    replacement_response_ref: Digest | None = Field(default=None, exclude_if=lambda v: v is None)
    intervention_id: Identifier | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def readonly_observation(self) -> "HybridCall":
        if (
            self.intent.effect != EffectClass.READ_ONLY
            or self.intent.occurrence.provider
            not in ("tool:corpus_search/v1", "tool:source_read/v1")
            or self.intent.model_budget is not None
            or not self.saved.complete
            or (self.replacement_response_ref is None) != (self.intervention_id is None)
        ):
            raise ValueError("Hybrid 仅选择完整的受控只读工具调用，响应替换须绑定干预")
        return self


class FrozenHybridPolicy(Contract):
    source_run_id: Identifier
    from_commit: Digest
    until_commit: Digest
    validator_ref: Digest
    corpus_digest: Digest
    miss_policy: Literal["stop", "fixture"]
    calls: Annotated[tuple[HybridCall, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def exact_slots(self) -> "FrozenHybridPolicy":
        keys = [
            (
                call.intent.occurrence.logical_step_id,
                call.intent.occurrence.call_index,
                call.intent.occurrence.attempt,
            )
            for call in self.calls
        ]
        if len(set(keys)) != len(keys) or len({call.source_call_id for call in self.calls}) != len(
            keys
        ):
            raise ValueError("Hybrid 的目标 occurrence 或来源调用重复")
        return self
