"""S10 证据内核契约；实际归属、执行和确认池开启仍由持久服务负责。"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.interventions import Operator
from agentgit.core.models import Contract, Digest, Identifier, NonNegative
from agentgit.core.repair import RepairTrialUsage


def validation_ref(value: Contract) -> str:
    return object_digest(ObjectType.BLOB, encode(value.model_dump(mode="json")))


class RepairAtom(Contract):
    patch_ref: Digest
    operator: Operator


class RepairCandidate(Contract):
    kind: Literal["repair_validation_candidate/v1"] = "repair_validation_candidate/v1"
    binding_ref: Digest
    source_proposal_ref: Digest
    task_ref: Digest
    atoms: Annotated[tuple[RepairAtom, ...], Field(max_length=8)]
    model_scope: Literal["deterministic_fixture", "stochastic_fixture", "provider"]

    @model_validator(mode="after")
    def distinct_atoms(self) -> "RepairCandidate":
        if len({atom.patch_ref for atom in self.atoms}) != len(self.atoms):
            raise ValueError("候选不能包含重复原子补丁")
        return self


class ValidationSample(Contract):
    kind: Literal["repair_validation_sample/v1"] = "repair_validation_sample/v1"
    role: Literal["reduce", "confirm", "regression"]
    task_ref: Digest
    response_set_ref: Digest
    validator_ref: Digest


class ValidationTrialEvidence(Contract):
    phase: Literal["reduce", "confirm", "regression"]
    protocol_ref: Digest
    trial_id: Identifier
    run_id: Identifier
    result_ref: Digest
    candidate_ref: Digest
    sample_ref: Digest
    validator_ref: Digest
    verdict: Literal["passed", "failed", "incomplete"]
    category: Literal["oracle", "execution", "budget", "cancelled", "unknown", "safety"]
    safety_violations: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=500)], ...], Field(max_length=32)
    ] = ()
    usage: RepairTrialUsage | None

    @model_validator(mode="after")
    def explicit_outcome(self) -> "ValidationTrialEvidence":
        if self.usage is not None and self.usage.cost_basis == "recorded_no_external_calls":
            raise ValueError("Recorded 恢复证明不能充当新的缩减/确认/回归试验")
        if (self.category == "oracle") != (self.verdict in ("passed", "failed")):
            raise ValueError("只有固定硬验收可以给出确定通过或失败，其他结论必须不完整")
        if self.verdict == "passed" and self.safety_violations:
            raise ValueError("有安全违规的试验不能作为通过证据")
        if self.category == "safety" and not self.safety_violations:
            raise ValueError("安全停止必须包含具体违规证据")
        return self


class DeletionRequest(Contract):
    before_ref: Digest
    removed_patch_ref: Digest
    after: RepairCandidate
    sample_ref: Digest
    validator_ref: Digest


class DeletionStep(DeletionRequest):
    evidence: ValidationTrialEvidence


class ReductionState(Contract):
    kind: Literal["repair_deletion_state/v1"] = "repair_deletion_state/v1"
    initial: RepairCandidate
    sample: ValidationSample
    screening_run_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=64)]
    screening_result_refs: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=65)]
    max_trials: Annotated[int, Field(ge=0, le=100)] = 6
    steps: Annotated[tuple[DeletionStep, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def reduction_scope(self) -> "ReductionState":
        if self.sample.role != "reduce" or self.sample.task_ref != self.initial.task_ref:
            raise ValueError("删除测试必须使用当前任务的固定缩减样本")
        if len(self.steps) > self.max_trials:
            raise ValueError("删除记录不能超过冻结额度")
        if len(set(self.screening_run_ids)) != len(self.screening_run_ids) or len(
            set(self.screening_result_refs)
        ) != len(self.screening_result_refs):
            raise ValueError("筛选证据身份不能重复")
        return self


class ReductionSummary(Contract):
    state_ref: Digest
    final_candidate: RepairCandidate
    trials: NonNegative
    removed_atoms: NonNegative
    tested_final_deletions: Annotated[tuple[Digest, ...], Field(max_length=8)]
    untested_final_deletions: Annotated[tuple[Digest, ...], Field(max_length=8)]
    indeterminate_final_deletions: Annotated[tuple[Digest, ...], Field(max_length=8)]
    status: Literal["running", "complete", "budget_exhausted", "needs_review", "cancelled"]
    reason: Literal[
        "eligible_deletions_remain",
        "declared_deletions_complete",
        "phase_trials",
        "trial_budget",
        "unresolved_trial",
        "safety_violation",
        "operator_cancelled",
        "indeterminate_deletions",
    ]
    minimality: Literal["not_proven", "one_minimal_under_fixture", "empirical", "empty_candidate"]
    minimality_scope: Literal["ordered_atomic_patch_deletions/v1"] = (
        "ordered_atomic_patch_deletions/v1"
    )
    global_optimum: Literal[False] = False
    field_reduction: Literal["not_run"] = "not_run"
    safety_violations: Annotated[tuple[str, ...], Field(max_length=3200)] = ()


class FinalRepairLock(Contract):
    kind: Literal["final_repair_lock/v1"] = "final_repair_lock/v1"
    candidate: RepairCandidate
    selection_event_ref: Digest
    selection_result_refs: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=165)]
    prior_run_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=164)]
    prior_safety_violations: Annotated[tuple[str, ...], Field(max_length=3200)] = ()

    @model_validator(mode="after")
    def unique_selection(self) -> "FinalRepairLock":
        if len(set(self.selection_result_refs)) != len(self.selection_result_refs) or len(
            set(self.prior_run_ids)
        ) != len(self.prior_run_ids):
            raise ValueError("候选锁定的来源证据不能重复")
        return self


class RepairSamplePool(Contract):
    kind: Literal["repair_validation_pool/v1"] = "repair_validation_pool/v1"
    screen_reduce_response_refs: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=1000)]
    confirm: Annotated[tuple[ValidationSample, ...], Field(max_length=6)] = ()
    regression: Annotated[tuple[ValidationSample, ...], Field(max_length=4)] = ()

    @model_validator(mode="after")
    def independent_roles(self) -> "RepairSamplePool":
        groups = (
            self.screen_reduce_response_refs,
            tuple(s.response_set_ref for s in self.confirm),
            tuple(s.response_set_ref for s in self.regression),
        )
        if any(len(set(refs)) != len(refs) for refs in groups):
            raise ValueError("同一阶段的响应样本不能重复")
        if any(set(groups[a]) & set(groups[b]) for a, b in ((0, 1), (0, 2), (1, 2))):
            raise ValueError("确认/回归响应不能通过改名或改 split 复用其他阶段内容")
        if any(s.role != "confirm" for s in self.confirm) or any(
            s.role != "regression" for s in self.regression
        ):
            raise ValueError("样本池角色与阶段不符")
        if len({s.task_ref for s in self.regression}) != len(self.regression):
            raise ValueError("固定回归必须使用不同任务，不能重复一个任务充数")
        return self


class RepairValidationPlan(Contract):
    kind: Literal["repair_validation_plan/v1"] = "repair_validation_plan/v1"
    policy: Literal["delete-then-confirm/v1"] = "delete-then-confirm/v1"
    lock: FinalRepairLock
    pool: RepairSamplePool
    opening_event_ref: Digest
    opened_for_lock_ref: Digest
    required_confirm: Literal[6] = 6
    minimum_confirm_passes: Literal[5] = 5
    required_regression: Literal[4] = 4

    @model_validator(mode="after")
    def same_task_confirmation(self) -> "RepairValidationPlan":
        if self.opened_for_lock_ref != validation_ref(self.lock):
            raise ValueError("已打开的响应池只能用于当时锁定的候选，不能换候选继续筛选")
        if any(s.task_ref != self.lock.candidate.task_ref for s in self.pool.confirm):
            raise ValueError("独立确认必须属于已经锁定的任务，任务层保留划分另行管理")
        return self


ProbabilityText = Annotated[str, Field(pattern=r"^(0(?:\.[0-9]{1,30})?|1(?:\.0{1,30})?)$")]


class WilsonInterval(Contract):
    method: Literal["wilson-score/v1"] = "wilson-score/v1"
    confidence: Literal["0.95"] = "0.95"
    z: Literal["1.95996398454005423552"] = "1.95996398454005423552"
    successes: NonNegative
    samples: Annotated[int, Field(ge=1)]
    lower: ProbabilityText
    upper: ProbabilityText

    @model_validator(mode="after")
    def bounded_interval(self) -> "WilsonInterval":
        if self.successes > self.samples or Decimal(self.lower) > Decimal(self.upper):
            raise ValueError("区间和样本计数无效")
        return self


class RepairVerificationSummary(Contract):
    kind: Literal["repair_verification_summary/v1"] = "repair_verification_summary/v1"
    plan_ref: Digest
    lock_ref: Digest
    result_refs: Annotated[tuple[Digest, ...], Field(max_length=10)]
    status: Literal["needs_review", "rejected", "verified_under_fixture", "empirically_confirmed"]
    confirm_planned: Literal[6] = 6
    confirm_available: Annotated[int, Field(ge=0, le=6)]
    confirm_attempted: Annotated[int, Field(ge=0, le=6)]
    confirm_determinate: Annotated[int, Field(ge=0, le=6)]
    confirm_passed: Annotated[int, Field(ge=0, le=6)]
    confirmation_interval: WilsonInterval | None
    regression_planned: Literal[4] = 4
    regression_available: Annotated[int, Field(ge=0, le=4)]
    regression_attempted: Annotated[int, Field(ge=0, le=4)]
    regression_passed: Annotated[int, Field(ge=0, le=4)]
    reasons: Annotated[tuple[str, ...], Field(max_length=32)]
    usage_scope: Literal["confirm_regression_only"] = "confirm_regression_only"
    unknown_usage_trials: Annotated[int, Field(ge=0, le=10)]
    known_model_calls: NonNegative
    known_tool_calls: NonNegative
    known_mock_utf8_bytes: NonNegative
    known_provider_tokens: NonNegative
    known_mixed_units: NonNegative
    known_cost_micro_usd: NonNegative
    cost_bases: Annotated[
        tuple[Literal["local_mock_free", "pricebook_estimate"], ...], Field(max_length=2)
    ]
    has_observation_patch: bool
    production_eligible: bool
    adoption: Literal["not_reviewed"] = "not_reviewed"
    global_optimum: Literal[False] = False
    test_only: Literal[True] = True
    limitations: Annotated[tuple[str, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def never_promote_incomplete_evidence(self) -> "RepairVerificationSummary":
        if (
            not 0
            <= self.confirm_passed
            <= self.confirm_determinate
            <= self.confirm_attempted
            <= self.confirm_available
        ):
            raise ValueError("确认范围与计数不符")
        if (
            not 0
            <= self.regression_passed
            <= self.regression_attempted
            <= self.regression_available
        ):
            raise ValueError("回归范围与计数不符")
        if (self.confirm_attempted == 0) != (self.confirmation_interval is None):
            raise ValueError("有且仅有确认观测时才能给出区间")
        if self.confirmation_interval is not None and (
            self.confirmation_interval.samples != self.confirm_attempted
            or self.confirmation_interval.successes != self.confirm_passed
        ):
            raise ValueError("区间必须包含实际全部确认尝试，不能丢弃失败或未知")
        successful = self.status in ("verified_under_fixture", "empirically_confirmed")
        if len(self.result_refs) != self.confirm_attempted + self.regression_attempted or len(
            set(self.result_refs)
        ) != len(self.result_refs):
            raise ValueError("验证摘要必须逐一关联全部实际试验结果，不能重复或漏掉失败")
        if successful and (
            self.confirm_determinate != 6
            or self.confirm_passed < 5
            or self.regression_passed != 4
            or self.reasons
            or self.unknown_usage_trials
        ):
            raise ValueError("完整确认、回归与安全条件不足时不能标记验证通过")
        if self.production_eligible != (successful and not self.has_observation_patch):
            raise ValueError("未验证或观测替换提案不能进入正式配置采纳")
        return self
