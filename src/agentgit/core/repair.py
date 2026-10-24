"""有限修复空间及分阶段预算；不包含执行、确认或生产配置写入。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.debug_reports import FailureAssertion
from agentgit.core.experiments import FrozenAssessment
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.interventions import NewIntervention
from agentgit.core.models import Contract, Digest, Identifier, NonNegative
from agentgit.core.research import BudgetLimits

RepairPhase = Literal["screen", "reduce", "confirm", "regression"]
TemplateName = Literal[
    "prompt-verified-sources/v1",
    "prompt-required-artifacts/v1",
    "plan-required-artifacts/v1",
    "recorded-observation/v1",
]


class RepairPatch(Contract):
    template: TemplateName
    intervention: NewIntervention

    @model_validator(mode="after")
    def template_operator(self) -> "RepairPatch":
        expected = (
            "PROMPT_PATCH"
            if self.template.startswith("prompt-")
            else "PLAN_PATCH"
            if self.template.startswith("plan-")
            else "TOOL_RESPONSE_PATCH"
        )
        if self.intervention.operator != expected:
            raise ValueError("修复模板与干预类型不符")
        return self


class RepairBinding(Contract):
    repository_id: Identifier
    failed_run_id: Identifier
    base_commit: Digest
    environment_ref: Digest
    validator_ref: Digest
    policy_version: Annotated[str, Field(min_length=1, max_length=100)] = "bounded-templates/v1"


class RepairSpace(Contract):
    binding: RepairBinding
    candidate_source: Literal["full_candidates_fallback", "static_ranking"]
    source_reason: Annotated[str, Field(min_length=1, max_length=2000)]
    patches: Annotated[tuple[RepairPatch, ...], Field(max_length=8)]
    excluded_patch_refs: Annotated[tuple[Digest, ...], Field(max_length=1024)] = ()
    max_depth: Annotated[int, Field(ge=1, le=2)] = 2

    @model_validator(mode="after")
    def common_origin(self) -> "RepairSpace":
        origins = set()
        for patch in self.patches:
            request = patch.intervention
            if (
                request.source_run_id != self.binding.failed_run_id
                or request.base_commit != self.binding.base_commit
                or request.precondition.validator_ref != self.binding.validator_ref
            ):
                raise ValueError("搜索原子补丁必须属于冻结来源、起点和验收器")
            origins.add((request.precondition.state_ref, request.precondition.config_ref))
        if len(origins) > 1:
            raise ValueError("搜索原子补丁必须使用共同起点状态和配置")
        return self


class RepairTrialIdentity(Contract):
    search_id: Identifier
    proposal_ref: Digest
    phase: RepairPhase
    repeat: Annotated[int, Field(ge=0, le=100)]


class PhaseQuotas(Contract):
    screen: Annotated[int, Field(ge=0, le=100)] = 14
    reduce: Annotated[int, Field(ge=0, le=100)] = 6
    confirm: Annotated[int, Field(ge=0, le=100)] = 6
    regression: Annotated[int, Field(ge=0, le=100)] = 4

    def total(self) -> int:
        return self.screen + self.reduce + self.confirm + self.regression


class RepairBudget(Contract):
    quotas: PhaseQuotas = PhaseQuotas()
    max_trials: Annotated[int, Field(ge=1, le=400)] = 30
    trial: BudgetLimits = BudgetLimits(
        max_model_calls=6,
        max_tool_calls=12,
        max_tokens=4000,
        max_wall_ms=60_000,
        max_cost_micro_usd=0,
    )
    max_model_calls: NonNegative = 180
    max_tool_calls: NonNegative = 360
    max_tokens: NonNegative = 60_000
    max_wall_ms: Annotated[int, Field(ge=1, le=900_000)] = 900_000
    max_concurrency: Annotated[int, Field(ge=1, le=4)] = 1
    currency: Literal["USD"] = "USD"
    max_cost_micro_usd: NonNegative = 0
    pricebook: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    @model_validator(mode="after")
    def bounded(self) -> "RepairBudget":
        if self.quotas.total() > self.max_trials:
            raise ValueError("阶段次数总和不能超过搜索 trial 上限")
        if self.trial.max_cost_micro_usd is None:
            raise ValueError("修复试验必须声明有限费用上限，Mock 为零")
        if bool(self.max_cost_micro_usd) != bool(self.pricebook):
            raise ValueError("非零费用必须且仅其需要冻结价格表版本")
        if self.trial.max_cost_micro_usd > self.max_cost_micro_usd:
            raise ValueError("单试验费用不能超过总费用")
        return self


class RepairConsumption(Contract):
    """已结算用量；未决试验另以完整 trial 上限预留，不填成零消耗。"""

    started: PhaseQuotas = PhaseQuotas(screen=0, reduce=0, confirm=0, regression=0)
    active: NonNegative = 0
    model_calls: NonNegative = 0
    tool_calls: NonNegative = 0
    tokens: NonNegative = 0
    cost_micro_usd: NonNegative = 0
    elapsed_wall_ms: NonNegative = 0

    @model_validator(mode="after")
    def active_was_started(self) -> "RepairConsumption":
        if self.active > self.started.total():
            raise ValueError("未决试验不能超过已启动数")
        return self


class BudgetDecision(Contract):
    allowed: bool
    reason: Literal[
        "available",
        "phase_trials",
        "total_trials",
        "concurrency",
        "model_calls",
        "tool_calls",
        "tokens",
        "cost",
        "wall_time",
    ]
    protected_trials: NonNegative


class RepairSearchSpec(Contract):
    space: RepairSpace
    budget: RepairBudget
    intervention_ids: Annotated[tuple[Identifier, ...], Field(max_length=8)]
    hybrid: HybridPolicy | None = None


class RepairSearchManifest(RepairSearchSpec):
    kind: Literal["bounded_repair_search/v1"] = "bounded_repair_search/v1"
    source_head_commit: Digest
    baseline: FrozenAssessment
    failure_assertions: Annotated[tuple[FailureAssertion, ...], Field(min_length=1, max_length=100)]
    code_revision: Annotated[str, Field(pattern=r"^source-sha256:[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def frozen_conditions(self) -> "RepairSearchManifest":
        binding = self.space.binding
        if len(self.intervention_ids) != len(self.space.patches) or len(
            set(self.intervention_ids)
        ) != len(self.intervention_ids):
            raise ValueError("原子补丁必须逐项绑定不同的已发布 S7 干预")
        if (
            self.baseline.verdict != "failed"
            or self.baseline.run_id != binding.failed_run_id
            or self.baseline.commit_id != self.source_head_commit
            or self.baseline.validator_ref != binding.validator_ref
        ):
            raise ValueError("修复必须绑定冻结验收器核验的原失败终态")
        if self.hybrid is None and any(
            patch.intervention.operator == "TOOL_RESPONSE_PATCH" for patch in self.space.patches
        ):
            raise ValueError("测试观测补丁必须绑定明确 Hybrid 策略")
        return self


class RepairProposal(Contract):
    key: Digest
    ordered_indices: Annotated[
        tuple[Annotated[int, Field(ge=0, le=7)], ...], Field(min_length=1, max_length=2)
    ]
    intervention_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=2)]
    patch_refs: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=2)]
    hybrid_ref: Digest | None = None

    @model_validator(mode="after")
    def ordered_bindings(self) -> "RepairProposal":
        if not len(self.ordered_indices) == len(self.intervention_ids) == len(self.patch_refs):
            raise ValueError("提案的有序索引、干预和效果摘要必须逐项匹配")
        if len(set(self.ordered_indices)) != len(self.ordered_indices):
            raise ValueError("提案不能重复应用同一原子补丁")
        return self


class RepairLease(Contract):
    repository_id: Identifier
    search_id: Identifier
    token: Identifier
    worker: Annotated[str, Field(min_length=1, max_length=200)]
    expires_at: datetime


class RepairTrialIntent(RepairTrialIdentity):
    ordinal: Annotated[int, Field(ge=0, le=399)]
    operation: Literal["recorded_preflight", "screen"]
    branch_name: Annotated[str, Field(pattern=r"^repair-[0-9a-f]{32}-[0-9]{1,3}$")]
    limits: BudgetLimits

    @model_validator(mode="after")
    def only_screening(self) -> "RepairTrialIntent":
        if self.phase != "screen" or self.repeat != 0:
            raise ValueError("S9 仅提交独立筛选；复测身份保留给 S10")
        return self
