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
    template_skips: Annotated[
        tuple[Annotated[str, Field(max_length=500)], ...], Field(max_length=32)
    ] = ()
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


class RepairTemplateTarget(Contract):
    event_id: Annotated[int, Field(ge=1)]
    replacement_call_ids: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def distinct_recordings(self) -> "RepairTemplateTarget":
        if len(set(self.replacement_call_ids)) != len(self.replacement_call_ids):
            raise ValueError("候选录制不能重复")
        return self


class NewRepairSearch(Contract):
    kind: Literal["template_repair_search/v1"] = "template_repair_search/v1"
    failed_run_id: Identifier
    base_commit: Digest
    validator_ref: Digest
    targets: Annotated[tuple[RepairTemplateTarget, ...], Field(min_length=1, max_length=8)]
    max_atoms: Annotated[int, Field(ge=1, le=8)] = 8
    max_depth: Annotated[int, Field(ge=1, le=2)] = 2
    source_reason: Annotated[str, Field(min_length=1, max_length=2000)] = (
        "定位尚未达标；使用给定共同起点与目标的完整有限模板候选，明确记录上限排除项。"
    )
    budget: RepairBudget | None = None
    hybrid: HybridPolicy | None = None
    fixture_scope: Literal["stochastic_fixture"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def distinct_targets(self) -> "NewRepairSearch":
        if len({target.event_id for target in self.targets}) != len(self.targets):
            raise ValueError("搜索目标事件不能重复")
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
    fixture_scope: Literal["stochastic_fixture"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


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


class RepairTrialUsage(Contract):
    model_calls: NonNegative
    tool_calls: NonNegative
    tokens: NonNegative
    cost_micro_usd: NonNegative
    unit: Literal["mock_utf8_bytes", "provider_tokens", "mixed_units"]
    cost_basis: Literal["local_mock_free", "pricebook_estimate", "recorded_no_external_calls"]


class RepairTrialResult(Contract):
    kind: Literal["repair_trial_result/v1"] = "repair_trial_result/v1"
    trial_id: Identifier
    job_id: Identifier
    initial_run_id: Identifier
    current_run_id: Identifier
    operation: Literal["recorded_preflight", "screen"]
    verdict: Literal["chain_valid", "passed", "failed"]
    category: Literal["recovery", "oracle", "execution", "budget", "cancelled", "safety"]
    determinate: bool
    usage: RepairTrialUsage
    replay_report_ref: Digest
    assessment: FrozenAssessment | None = None
    safety_violations: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    uncertainties: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    test_only: Literal[True] = True

    @model_validator(mode="after")
    def bound_outcome(self) -> "RepairTrialResult":
        if self.operation == "recorded_preflight":
            if self.assessment is not None or self.verdict == "passed":
                raise ValueError("Recorded 恢复证明不能冒充修复硬验收通过")
            if any(
                (
                    self.usage.model_calls,
                    self.usage.tool_calls,
                    self.usage.tokens,
                    self.usage.cost_micro_usd,
                )
            ):
                raise ValueError("Recorded 不会产生新的外部调用或供应商费用")
            if self.category == "oracle" or (
                self.verdict == "chain_valid"
                and (not self.determinate or self.category != "recovery")
            ):
                raise ValueError("恢复链通过必须是确定的恢复证明，不能替代任务验收")
        elif self.verdict == "chain_valid":
            raise ValueError("筛选试验不能声称是 Recorded 恢复证明")
        elif self.category == "recovery":
            raise ValueError("筛选结果不能标记为历史恢复证明")
        if self.assessment is not None and self.assessment.run_id != self.current_run_id:
            raise ValueError("结果验收必须绑定实际当前 Run")
        if self.verdict == "passed" and (
            self.assessment is None
            or self.assessment.verdict != "passed"
            or self.safety_violations
            or not self.determinate
            or self.category != "oracle"
        ):
            raise ValueError("只有确定硬验收通过且零安全违规的筛选可以通过")
        if self.category in ("budget", "cancelled", "safety") and self.determinate:
            raise ValueError("预算、取消或安全停止不能算作完整空间的确定失败")
        if (
            self.category == "oracle"
            and self.determinate
            and (self.assessment is None or self.assessment.verdict != self.verdict)
        ):
            raise ValueError("确定的任务结论必须与独立冻结验收一致")
        return self


class RepairCandidateComparison(Contract):
    proposal_id: Identifier
    proposal_key: Digest
    trial_id: Identifier
    result_ref: Digest
    ordinal: Annotated[int, Field(ge=0, le=63)]
    verdict: Literal["passed", "failed"]
    category: Literal["oracle", "execution", "budget", "cancelled", "safety"]
    determinate: bool
    safety_violations: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    patch_count: Annotated[int, Field(ge=1, le=2)]
    change_bytes: NonNegative
    usage: RepairTrialUsage
    wall_ms: NonNegative | None

    def rank_key(self) -> tuple[int, ...]:
        return (
            int(bool(self.safety_violations)),
            0 if self.verdict == "passed" else 1 if self.determinate else 2,
            self.patch_count,
            self.change_bytes,
            self.usage.tokens,
            self.usage.cost_micro_usd,
            int(self.wall_ms is None),
            self.wall_ms or 0,
            self.ordinal,
        )

    @model_validator(mode="after")
    def comparable_evidence(self) -> "RepairCandidateComparison":
        if self.verdict == "passed" and (
            not self.determinate or self.category != "oracle" or self.safety_violations
        ):
            raise ValueError("候选排序不能把不确定或不安全结果列为通过")
        if self.determinate and self.category != "oracle":
            raise ValueError("只有固定硬验收结果可列为确定结论")
        return self


class RepairSearchDecision(Contract):
    kind: Literal["bounded_repair_decision/v1"] = "bounded_repair_decision/v1"
    search_id: Identifier
    manifest_ref: Digest
    policy_version: Annotated[str, Field(min_length=1, max_length=100)]
    status: Literal[
        "needs_confirmation",
        "budget_exhausted",
        "no_solution_in_space",
        "not_found",
        "needs_attention",
        "cancelled",
    ]
    reason: Annotated[str, Field(min_length=1, max_length=200)]
    total_proposals: NonNegative
    explored_proposals: NonNegative
    unexplored_proposals: NonNegative
    passed_proposals: NonNegative
    determinate_failures: NonNegative
    excluded_atoms: NonNegative
    result_refs: Annotated[tuple[Digest, ...], Field(max_length=65)]
    comparison: Annotated[tuple[RepairCandidateComparison, ...], Field(max_length=64)] | None = None
    selected_proposal_id: Identifier | None = None
    selected_proposal_key: Digest | None = None
    selected_result_ref: Digest | None = None
    selected_change_bytes: NonNegative | None = None
    change_metric: Literal["canonical-change-span/v1"] = "canonical-change-span/v1"
    usage: RepairConsumption
    confirmation: Literal["not_run"] = "not_run"
    minimality: Literal["not_tested"] = "not_tested"
    global_optimum: Literal[False] = False
    uncertainties: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    test_only: Literal[True] = True

    @model_validator(mode="after")
    def scoped_conclusion(self) -> "RepairSearchDecision":
        if self.usage.active or len(set(self.result_refs)) != len(self.result_refs):
            raise ValueError("停止决定必须已经排空在途项且不能重复计算结果")
        if self.total_proposals != self.explored_proposals + self.unexplored_proposals:
            raise ValueError("停止报告必须完整解释声明空间")
        if self.passed_proposals + self.determinate_failures > self.explored_proposals:
            raise ValueError("通过或确定失败不能超过已探索候选")
        selected = (
            self.selected_proposal_id,
            self.selected_proposal_key,
            self.selected_result_ref,
            self.selected_change_bytes,
        )
        if any(value is not None for value in selected) != all(
            value is not None for value in selected
        ):
            raise ValueError("最终候选必须完整绑定提案、结果和变更幅度")
        if self.status == "needs_confirmation" and self.selected_proposal_id is None:
            raise ValueError("待确认必须有筛选通过候选")
        if self.selected_proposal_id is not None and not self.passed_proposals:
            raise ValueError("不能选择未通过筛选的候选")
        if self.status == "no_solution_in_space" and (
            not self.total_proposals
            or self.unexplored_proposals
            or self.passed_proposals
            or self.determinate_failures != self.total_proposals
            or self.selected_proposal_id is not None
        ):
            raise ValueError("只有声明空间全部确定失败才能称该空间内无解")
        if self.comparison is not None:
            rows = self.comparison
            if (
                len(rows) != self.explored_proposals
                or len({row.proposal_id for row in rows}) != len(rows)
                or len({row.proposal_key for row in rows}) != len(rows)
                or len({row.trial_id for row in rows}) != len(rows)
                or len({row.result_ref for row in rows}) != len(rows)
                or len({row.ordinal for row in rows}) != len(rows)
                or any(row.result_ref not in self.result_refs for row in rows)
                or tuple(sorted(rows, key=lambda row: row.rank_key())) != rows
                or sum(row.verdict == "passed" for row in rows) != self.passed_proposals
                or sum(row.verdict == "failed" and row.determinate for row in rows)
                != self.determinate_failures
                or any(
                    sum(getattr(row.usage, name) for row in rows) != getattr(self.usage, name)
                    for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                )
            ):
                raise ValueError("排序必须完整绑定已探索结果、计数与冻结字典序")
            best = next((row for row in rows if row.verdict == "passed"), None)
            if best is not None and selected != (
                best.proposal_id,
                best.proposal_key,
                best.result_ref,
                best.change_bytes,
            ):
                raise ValueError("选中候选必须是冻结排序中的首个通过项")
        return self
