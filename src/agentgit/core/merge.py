"""S11 只读合并候选的有界证据与冲突；不包含批准或执行权限。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.execution import ExecutionPolicy
from agentgit.core.models import Contract, Digest, EffectClass, Identifier
from agentgit.core.research import (
    BudgetLimits,
    BudgetUsage,
    CallTrace,
    Evaluation,
    Evidence,
    Plan,
    PlanNode,
    ResearchGoal,
)


class ConflictKind(StrEnum):
    WRITE_WRITE = "WRITE_WRITE"
    FACT_CONTRADICTION = "FACT_CONTRADICTION"
    TEMPORAL_MISMATCH = "TEMPORAL_MISMATCH"
    PLAN_DEPENDENCY = "PLAN_DEPENDENCY"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"
    EFFECT_CONFLICT = "EFFECT_CONFLICT"
    PROVENANCE_MISSING = "PROVENANCE_MISSING"


class MergeConflict(Contract):
    kind: ConflictKind
    paths: Annotated[tuple[str, ...], Field(min_length=1, max_length=10)]
    branches: tuple[Literal["base", "ours", "theirs"], ...] = ("base", "ours", "theirs")
    evidence_refs: Annotated[tuple[Digest, ...], Field(max_length=3)] = ()
    severity: Literal["blocking"] = "blocking"
    proposed_resolutions: tuple[str, ...] = ("人工编辑候选后重新验证",)


class MergeBases(Contract):
    ours: Digest
    theirs: Digest
    bases: Annotated[tuple[Digest, ...], Field(max_length=10000)]
    selected: Digest | None = None
    status: Literal["selected", "ambiguous_merge_base", "unrelated"]

    @model_validator(mode="after")
    def coherent_selection(self) -> "MergeBases":
        if self.bases != tuple(sorted(set(self.bases))):
            raise ValueError("最佳共同祖先必须去重并稳定排序")
        if self.selected is not None and self.selected not in self.bases:
            raise ValueError("选定基点不在最佳共同祖先内")
        expected = (
            "selected"
            if self.selected is not None
            else ("ambiguous_merge_base" if len(self.bases) > 1 else "unrelated")
        )
        if self.status != expected or (len(self.bases) == 1 and self.selected is None):
            raise ValueError("共同祖先状态与选定结果不一致")
        return self


class MergeOrigin(Contract):
    run_id: Identifier
    commit_id: Digest


class ApprovedMergeOrigin(Contract):
    kind: Literal["approved_merge"] = "approved_merge"
    approval_id: Identifier
    commit_id: Digest


MergeSourceOrigin = MergeOrigin | ApprovedMergeOrigin


class MergeReadProof(Contract):
    origin: MergeOrigin
    call_id: Identifier
    call_run_id: Identifier
    evidence: Evidence
    trace: CallTrace


class MergeEffect(Contract):
    call_id: Identifier
    run_id: Identifier
    provider: str
    effect: EffectClass
    status: Literal["proposed", "started", "succeeded", "failed", "outcome_unknown"]
    intent_ref: Digest
    result_ref: Digest | None
    cleanup_confirmed: bool = False


class MergeConstraints(Contract):
    tools: Annotated[tuple[str, ...], Field(max_length=5)]
    limits: BudgetLimits
    execution_policy: ExecutionPolicy | None = None
    evaluation_time: str


class MergeCompletedClaim(Contract):
    origin: MergeOrigin
    node: PlanNode


class MergeSourceUsage(Contract):
    origin: MergeSourceOrigin
    usage: BudgetUsage | None
    execution: Literal["not_executed"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def exact_usage_kind(self) -> "MergeSourceUsage":
        if isinstance(self.origin, ApprovedMergeOrigin):
            if self.usage is not None or self.execution != "not_executed":
                raise ValueError("合并来源没有执行用量")
        elif self.usage is None or self.execution is not None:
            raise ValueError("实际 Run 来源须保留原用量")
        return self


class MergedResearchState(Contract):
    kind: Literal["merged_research_state/v1"] = "merged_research_state/v1"
    execution: Literal["requires_new_run"] = "requires_new_run"
    goal: ResearchGoal
    plan: Plan | None
    working_memory: Annotated[dict[str, Evidence], Field(max_length=1000)]
    constraints: MergeConstraints
    completed_audit: Annotated[tuple[MergeCompletedClaim, ...], Field(max_length=192)]
    read_proofs: Annotated[tuple[MergeReadProof, ...], Field(max_length=3000)]
    effects_audit: Annotated[tuple[MergeEffect, ...], Field(max_length=3000)]
    source_usage: Annotated[tuple[MergeSourceUsage, ...], Field(min_length=3, max_length=3)]


class MergeValidation(Contract):
    origins: Annotated[tuple[MergeSourceOrigin, ...], Field(min_length=3, max_length=3)]
    validator_ref: Digest
    candidate_digest: Digest
    code_revision: str
    dependency_lock_digest: Digest
    conflicts: Annotated[tuple[MergeConflict, ...], Field(max_length=1000)]
    report_evaluation: Evaluation | None
    validated: bool
    approval_required: Literal[True] = True

    @model_validator(mode="after")
    def valid_result(self) -> "MergeValidation":
        if any(not isinstance(origin, MergeOrigin) for origin in self.origins[1:]):
            raise ValueError("合并两侧必须绑定明确来源 Run")
        expected = (
            not self.conflicts
            and self.report_evaluation is not None
            and self.report_evaluation.passed
        )
        if self.validated != expected:
            raise ValueError("合并验证状态与冲突及硬规则结果不一致")
        return self
