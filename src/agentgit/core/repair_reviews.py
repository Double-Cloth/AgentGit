"""精确修复建议、可刷新审阅票据和不可变决定，均不承担执行权限。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.experiments import ValidatorManifest
from agentgit.core.models import Contract, Digest, EnvironmentManifest, Identifier
from agentgit.core.repair import RepairBudget, RepairConsumption, RepairPatch
from agentgit.core.repair_fields import RepairFieldSummary
from agentgit.core.repair_validation import (
    FinalRepairLock,
    ReductionSummary,
    RepairVerificationSummary,
    validation_ref,
)
from agentgit.core.research import AgentConfig, ResearchGoal, evolve


class RepairAdoptionProposal(Contract):
    kind: Literal["repair_adoption_proposal/v1"] = "repair_adoption_proposal/v1"
    study_id: Identifier
    search_id: Identifier
    source_proposal_id: Identifier
    source_manifest_ref: Digest
    source_config_id: Identifier
    source_config_version: Annotated[int, Field(ge=1)]
    source_config_ref: Digest
    source_environment_ref: Digest
    source_environment: EnvironmentManifest
    source_goal: ResearchGoal
    validator_ref: Digest
    validator: ValidatorManifest
    search_budget: RepairBudget
    lock: FinalRepairLock
    verification_ref: Digest
    verification: RepairVerificationSummary
    before: AgentConfig
    after: AgentConfig | None
    patches: Annotated[tuple[RepairPatch, ...], Field(max_length=8)]
    reduction: ReductionSummary
    fields: RepairFieldSummary | None
    usage: RepairConsumption
    code_revision: Annotated[str, Field(pattern=r"^source-sha256:[0-9a-f]{64}$")]
    changed_fields: Annotated[tuple[Literal["prompt", "plan_template"], ...], Field(max_length=2)]
    adoption_scope: Literal["create_config_version", "save_experiment_only"]

    @model_validator(mode="after")
    def exact_content(self) -> "RepairAdoptionProposal":
        if (
            self.verification.lock_ref != validation_ref(self.lock)
            or validation_ref(self.verification) != self.verification_ref
            or self.usage.active
        ):
            raise ValueError("提案必须绑定已排空的确切候选锁与验证摘要")
        scoped = (
            self.fields.final_candidate
            if self.fields is not None
            else self.reduction.final_candidate
        )
        if scoped != self.lock.candidate:
            raise ValueError("提案缩减范围必须对应最终锁定候选")
        if validation_ref(self.before) != self.source_config_ref:
            raise ValueError("正式来源配置与提案原文摘要不符")
        if (
            object_digest(
                ObjectType.ENVIRONMENT, encode(self.source_environment.model_dump(mode="json"))
            )
            != self.source_environment_ref
            or validation_ref(self.validator) != self.validator_ref
            or self.validator.input_ref != self.lock.candidate.task_ref
            or validation_ref(self.source_goal) != self.lock.candidate.task_ref
            or self.validator.code_revision != self.code_revision
            or self.source_environment.code_revision != self.code_revision
            or self.validator.evaluation_time != self.source_environment.evaluation_time
        ):
            raise ValueError("提案必须展示同一冻结任务、验收器与适用环境")
        from agentgit.repair.space import patch_ref

        if tuple(patch_ref(p) for p in self.patches) != tuple(
            a.patch_ref for a in self.lock.candidate.atoms
        ):
            raise ValueError("精确提案必须逐一包含最终锁定补丁")
        observed = any(p.intervention.operator == "TOOL_RESPONSE_PATCH" for p in self.patches)
        if (
            observed != (self.adoption_scope == "save_experiment_only")
            or observed != self.verification.has_observation_patch
        ):
            raise ValueError("观测补丁只能保存实验，不能转为正式配置")
        projected = self.before
        for patch in self.patches:
            payload = patch.intervention.payload
            if payload.kind == "PROMPT_PATCH":
                projected = evolve(projected, prompt=payload.prompt)
            elif payload.kind == "PLAN_PATCH":
                projected = evolve(projected, plan_template=payload.plan)
        changed = tuple(
            k
            for k in ("prompt", "plan_template")
            if getattr(projected, k) != getattr(self.before, k)
        )
        if self.changed_fields != changed or self.after != (None if observed else projected):
            raise ValueError("提案不能改变未声明配置字段或回退派生补丁")
        return self


class RepairReviewTicket(Contract):
    kind: Literal["repair_review_ticket/v1"] = "repair_review_ticket/v1"
    proposal_id: Identifier
    proposal_ref: Digest
    expected_config_version: Annotated[int, Field(ge=1)]
    source_config_id: Identifier
    source_config_version: Annotated[int, Field(ge=1)]
    lock_ref: Digest
    verification_ref: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]


class RepairSavedExperiment(Contract):
    kind: Literal["repair_saved_experiment/v1"] = "repair_saved_experiment/v1"
    proposal_ref: Digest
    lock_ref: Digest
    verification_ref: Digest
    patches: Annotated[tuple[RepairPatch, ...], Field(max_length=8)]
    source_manifest_ref: Digest
    execution: Literal["not_started"] = "not_started"
    test_only: Literal[True] = True


class DecideRepairReview(Contract):
    review_id: Identifier
    proposal_ref: Digest
    expected_config_version: Annotated[int, Field(ge=1)]
    decision: Literal["approve", "reject", "save_experiment"]


class RepairReviewDecision(Contract):
    kind: Literal["repair_review_decision/v1"] = "repair_review_decision/v1"
    proposal_id: Identifier
    review_id: Identifier
    proposal_ref: Digest
    ticket_ref: Digest
    expected_config_version: Annotated[int, Field(ge=1)]
    decision: Literal["approve", "reject", "save_experiment"]
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    source_config_id: Identifier
    source_config_version: Annotated[int, Field(ge=1)]
    new_config_id: Identifier | None
    new_config_version: Annotated[int, Field(ge=1)] | None
    saved_experiment_id: Identifier | None
    lock_ref: Digest
    verification_ref: Digest
    execution: Literal["not_started"] = "not_started"

    @model_validator(mode="after")
    def decision_effect(self) -> "RepairReviewDecision":
        if (self.decision == "approve") != (
            self.new_config_id is not None
            and self.new_config_version == self.expected_config_version + 1
        ):
            raise ValueError("批准必须且仅创建一个预期的新配置版本")
        if self.decision != "approve" and (
            self.new_config_id is not None or self.new_config_version is not None
        ):
            raise ValueError("拒绝或保存实验不能创建配置版本")
        if (self.decision == "save_experiment") != (self.saved_experiment_id is not None):
            raise ValueError("只有保存实验决定能够创建实验定义引用")
        return self
