"""有限修复空间及分阶段预算；不包含执行、确认或生产配置写入。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

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
