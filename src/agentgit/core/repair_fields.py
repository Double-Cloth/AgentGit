"""锁前字段缩减的独立协议；历史原子删除状态保持原编码。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, Digest, Identifier, NonNegative
from agentgit.core.repair import BudgetDecision, RepairPatch
from agentgit.core.repair_confirmation import ValidationModelCall
from agentgit.core.repair_studies import RepairReductionReceipt
from agentgit.core.repair_validation import (
    RepairCandidate,
    ValidationSample,
    ValidationTrialEvidence,
)
from agentgit.core.research import AgentConfig, BudgetLimits, Plan

AtomicMinimality = Literal[
    "not_proven", "one_minimal_under_fixture", "empirical", "empty_candidate"
]


class RepairFieldPlan(Contract):
    kind: Literal["repair_field_plan/v1"] = "repair_field_plan/v1"
    policy: Literal["predefined-fields/v1"] = "predefined-fields/v1"
    parent_reduction_ref: Digest
    initial: RepairCandidate
    source_config: AgentConfig
    source_plan: Plan | None
    completed: Annotated[tuple[str, ...], Field(max_length=64)]
    patches: Annotated[tuple[RepairPatch, ...], Field(max_length=8)]
    sample: ValidationSample
    prior_run_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=164)]
    prior_result_refs: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=165)]
    initial_atomic_minimality: AtomicMinimality
    max_trials: Annotated[int, Field(ge=0, le=100)]
    max_variants: Literal[128] = 128
    parent_budget_stop: BudgetDecision | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def fixed_origin(self) -> "RepairFieldPlan":
        if self.parent_budget_stop is not None and (
            self.parent_budget_stop.allowed
            or self.parent_budget_stop.reason in ("available", "concurrency")
            or self.max_trials != 0
        ):
            raise ValueError("原子阶段预算停止必须保留实际拒绝且不追加字段额度")
        if self.sample.role != "reduce" or self.sample.task_ref != self.initial.task_ref:
            raise ValueError("字段缩减只能使用同一任务的固定 reduce 验收器")
        if len(set(self.completed)) != len(self.completed):
            raise ValueError("字段策略的已完成节点不能重复")
        if self.completed and (
            self.source_plan is None
            or any(
                key not in {node.key for node in self.source_plan.nodes} for key in self.completed
            )
        ):
            raise ValueError("已完成节点必须属于冻结源计划")
        if len(set(self.prior_run_ids)) != len(self.prior_run_ids) or len(
            set(self.prior_result_refs)
        ) != len(self.prior_result_refs):
            raise ValueError("字段阶段的已见来源身份不能重复")
        if (
            len(self.prior_run_ids) + self.max_trials > 164
            or len(self.prior_result_refs) + self.max_trials > 165
        ):
            raise ValueError("字段剩余额度不能超过最终候选锁的完整来源容量")
        if (
            self.initial_atomic_minimality == "one_minimal_under_fixture"
            and self.initial.model_scope != "deterministic_fixture"
        ):
            raise ValueError("随机或真实模型不能继承确定性 fixture 的原子最小性证明")
        if (self.initial_atomic_minimality == "empty_candidate") != (not self.initial.atoms):
            raise ValueError("空候选的必要性范围必须明确且匹配原子集合")
        return self


class RepairFieldVariant(Contract):
    policy: Literal["predefined-fields/v1"] = "predefined-fields/v1"
    root_patch_ref: Digest
    parent_patch_ref: Digest
    operation: Literal["prompt_clause", "plan_leaf", "plan_dependency"]
    location: Annotated[str, Field(min_length=1, max_length=250)]
    patch: RepairPatch
    before_bytes: NonNegative
    after_bytes: NonNegative

    @model_validator(mode="after")
    def strictly_smaller(self) -> "RepairFieldVariant":
        if self.after_bytes >= self.before_bytes:
            raise ValueError("预定义变换必须严格减少补丁 payload 字节")
        expected = "PROMPT_PATCH" if self.operation == "prompt_clause" else "PLAN_PATCH"
        if self.patch.intervention.operator != expected:
            raise ValueError("字段变换不能改变补丁操作类型")
        return self


class RepairFieldRequest(Contract):
    kind: Literal["field", "atomic_recheck"]
    before_ref: Digest
    changed_patch_ref: Digest
    after: RepairCandidate
    variant: RepairFieldVariant | None
    sample_ref: Digest
    validator_ref: Digest

    @model_validator(mode="after")
    def typed_change(self) -> "RepairFieldRequest":
        if (self.kind == "field") != (self.variant is not None):
            raise ValueError("字段替换必须有变换，原子复查不能附带字段修改")
        if self.variant is not None and self.variant.parent_patch_ref != self.changed_patch_ref:
            raise ValueError("变换父补丁必须是本次被修改的确切原子")
        return self


class RepairFieldStep(Contract):
    request: RepairFieldRequest
    evidence: ValidationTrialEvidence


class RepairFieldState(Contract):
    kind: Literal["repair_field_state/v1"] = "repair_field_state/v1"
    plan: RepairFieldPlan
    steps: Annotated[tuple[RepairFieldStep, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def bounded(self) -> "RepairFieldState":
        if len(self.steps) > self.plan.max_trials:
            raise ValueError("字段与必要性复查共同使用冻结的剩余次数")
        return self


class RepairFieldSummary(Contract):
    state_ref: Digest
    final_candidate: RepairCandidate
    status: Literal["running", "complete", "budget_exhausted", "needs_review", "cancelled"]
    reason: Annotated[str, Field(min_length=1, max_length=100)]
    field_trials: NonNegative
    atomic_recheck_trials: NonNegative
    accepted_fields: NonNegative
    removed_atoms: NonNegative
    atomic_minimality: AtomicMinimality
    field_minimality: Literal[
        "not_proven", "no_applicable_fields", "one_step_minimal_under_fixture", "empirical"
    ]
    tested_field_refs: Annotated[tuple[Digest, ...], Field(max_length=128)]
    pending_field_refs: Annotated[tuple[Digest, ...], Field(max_length=128)]
    pending_atom_refs: Annotated[tuple[Digest, ...], Field(max_length=8)]
    excluded_transform_count: NonNegative
    excluded_reasons: Annotated[tuple[str, ...], Field(max_length=256)]
    minimality_scope: Literal["bounded_predefined_fields_and_atomic_rechecks/v1"] = (
        "bounded_predefined_fields_and_atomic_rechecks/v1"
    )
    global_optimum: Literal[False] = False


class RepairFieldIntent(Contract):
    kind: Literal["repair_field_intent/v1"] = "repair_field_intent/v1"
    study_id: Identifier
    ordinal: Annotated[int, Field(ge=0, le=99)]
    phase: Literal["reduce"] = "reduce"
    protocol_ref: Digest
    source_manifest_ref: Digest
    request: RepairFieldRequest
    config: AgentConfig
    plan: Plan | None
    limits: BudgetLimits
    hybrid_ref: Digest | None
    branch_name: Annotated[str, Field(pattern=r"^field-[0-9a-f]{32}-[0-9]{1,2}$")]

    @model_validator(mode="after")
    def limits_match(self) -> "RepairFieldIntent":
        if self.config.limits != self.limits:
            raise ValueError("字段投影必须使用冻结的单 trial 预算")
        return self


class RepairFieldProof(Contract):
    kind: Literal["repair_field_execution_proof/v1"] = "repair_field_execution_proof/v1"
    trial_id: Identifier
    protocol_ref: Digest
    candidate_ref: Digest
    job_id: Identifier
    initial_run_id: Identifier
    current_run_id: Identifier
    run_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=100)]
    model_calls: Annotated[tuple[ValidationModelCall, ...], Field(max_length=1000)]

    @model_validator(mode="after")
    def owned_family(self) -> "RepairFieldProof":
        if (
            self.run_ids[0] != self.current_run_id
            or self.run_ids[-1] != self.initial_run_id
            or len(set(self.run_ids)) != len(self.run_ids)
        ):
            raise ValueError("字段调用只能属于本次独立 Job 的恢复链")
        origins = {call.call_id: call.origin_call_id for call in self.model_calls}
        if len(origins) != len(self.model_calls) or any(
            call.run_id not in self.run_ids
            or (call.origin_call_id is not None and call.origin_call_id not in origins)
            for call in self.model_calls
        ):
            raise ValueError("字段不能借用其他 Job 的模型响应")
        for call in self.model_calls:
            seen, parent = {call.call_id}, call.origin_call_id
            while parent is not None:
                if parent in seen:
                    raise ValueError("字段模型恢复来源不能成环")
                seen.add(parent)
                parent = origins[parent]
        return self


class RepairFieldReceipt(Contract):
    kind: Literal["repair_field_receipt/v1"] = "repair_field_receipt/v1"
    execution: RepairReductionReceipt
    provenance_ref: Digest | None
    fresh_model_calls: NonNegative

    @model_validator(mode="after")
    def actual_models(self) -> "RepairFieldReceipt":
        if self.execution.not_started != (self.provenance_ref is None):
            raise ValueError("实际字段试验必须保留来源证明，空取消不能伪造证明")
        if self.fresh_model_calls != self.execution.usage.model_calls:
            raise ValueError("字段物理模型用量必须与新调用来源一致")
        if self.execution.category == "oracle" and not self.fresh_model_calls:
            raise ValueError("字段验收不能用零新模型调用或旧产物充数")
        return self
