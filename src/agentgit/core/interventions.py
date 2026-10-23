"""三个受审阅干预操作；内容、目标和前置条件独立冻结。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import CallOccurrence, Contract, Digest, Identifier
from agentgit.core.research import Plan


class EventReference(Contract):
    run_id: Identifier
    seq: Annotated[int, Field(ge=1)]


class InterventionPrecondition(Contract):
    state_ref: Digest
    config_ref: Digest
    validator_ref: Digest
    occurrence: CallOccurrence


class PatchProvenance(Contract):
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence: Annotated[tuple[EventReference, ...], Field(max_length=32)] = ()


class PromptPatch(Contract):
    kind: Literal["PROMPT_PATCH"] = "PROMPT_PATCH"
    field: Literal["agent_config.prompt"] = "agent_config.prompt"
    prompt: Annotated[str, Field(min_length=1, max_length=16000)]


class PlanPatch(Contract):
    kind: Literal["PLAN_PATCH"] = "PLAN_PATCH"
    plan: Plan


class ToolResponsePatch(Contract):
    kind: Literal["TOOL_RESPONSE_PATCH"] = "TOOL_RESPONSE_PATCH"
    replacement_call_id: Identifier
    test_only: Literal[True] = True


PatchPayload = Annotated[PromptPatch | PlanPatch | ToolResponsePatch, Field(discriminator="kind")]
Operator = Literal["PROMPT_PATCH", "PLAN_PATCH", "TOOL_RESPONSE_PATCH"]
EffectPolicy = Literal["sandbox_or_approval", "test_only"]


class NewIntervention(Contract):
    source_run_id: Identifier
    base_commit: Digest
    target_commit_id: Digest
    target_event_id: Annotated[int, Field(ge=1)]
    operator: Operator
    payload: PatchPayload
    precondition: InterventionPrecondition
    effect_policy: EffectPolicy
    provenance: PatchProvenance

    @model_validator(mode="after")
    def matching_operator(self) -> "NewIntervention":
        if self.operator != self.payload.kind:
            raise ValueError("干预操作与 payload 类型不符")
        expected = "test_only" if self.operator == "TOOL_RESPONSE_PATCH" else "sandbox_or_approval"
        if self.effect_policy != expected:
            raise ValueError("干预不得改变既定效果策略")
        return self


class Intervention(Contract):
    source_run_id: Identifier
    base_commit: Digest
    target_commit_id: Digest
    target_event_id: Annotated[int, Field(ge=1)]
    target_event_seq: Annotated[int, Field(ge=1)]
    source_call_id: Identifier
    operator: Operator
    payload_ref: Digest
    precondition: InterventionPrecondition
    effect_policy: EffectPolicy
    provenance: PatchProvenance
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    replacement_response_ref: Digest | None = Field(default=None, exclude_if=lambda v: v is None)
