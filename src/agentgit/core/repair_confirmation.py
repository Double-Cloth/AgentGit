"""独立确认/回归的冻结执行投影、意图和实际收据。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.experiments import FrozenAssessment
from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.repair import RepairTrialUsage
from agentgit.core.repair_pools import RegisteredValidationSample
from agentgit.core.research import AgentConfig, BudgetLimits, Plan


class RepairValidationProjection(Contract):
    kind: Literal["repair_validation_projection/v1"] = "repair_validation_projection/v1"
    lock_ref: Digest
    ticket_ref: Digest
    config: AgentConfig
    plan: Plan | None
    hybrid_ref: Digest | None
    observation_scope: Literal["none", "selected_source", "outside_source"]
    unapplied_observation_atoms: Annotated[tuple[Digest, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def explicit_scope(self) -> "RepairValidationProjection":
        if (self.observation_scope == "outside_source") != bool(self.unapplied_observation_atoms):
            raise ValueError("观测替换在其他任务上无效时必须保留确切原子身份")
        if self.observation_scope == "selected_source" and self.hybrid_ref is None:
            raise ValueError("原来源的观测替换不能降级成 Live")
        return self


class RepairValidationIntent(Contract):
    kind: Literal["repair_validation_intent/v1"] = "repair_validation_intent/v1"
    study_id: Identifier
    sample_id: Identifier
    ordinal: Annotated[int, Field(ge=0, le=9)]
    phase: Literal["confirm", "regression"]
    protocol_ref: Digest
    lock_ref: Digest
    registered: RegisteredValidationSample
    projection_ref: Digest
    branch_name: Annotated[str, Field(pattern=r"^validate-[0-9a-f]{32}-[0-9]$")]
    limits: BudgetLimits

    @model_validator(mode="after")
    def ordered_sample(self) -> "RepairValidationIntent":
        if (
            self.phase != self.registered.sample.role
            or self.sample_id != self.registered.ticket.generation_id
            or (self.ordinal < 6) != (self.phase == "confirm")
        ):
            raise ValueError("确认意图必须属于已开启计划的正确样本和阶段")
        return self


class ValidationModelCall(Contract):
    call_id: Identifier
    run_id: Identifier
    origin_call_id: Identifier | None
    intent_ref: Digest
    result_ref: Digest | None


class ValidationExecutionProof(Contract):
    kind: Literal["repair_validation_execution_proof/v1"] = "repair_validation_execution_proof/v1"
    trial_id: Identifier
    sample_id: Identifier
    protocol_ref: Digest
    projection_ref: Digest
    job_id: Identifier
    initial_run_id: Identifier
    current_run_id: Identifier
    run_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=100)]
    model_calls: Annotated[tuple[ValidationModelCall, ...], Field(max_length=1000)]
    conditions_checked: Literal[True] = True

    @model_validator(mode="after")
    def owned_call_family(self) -> "ValidationExecutionProof":
        if (
            self.current_run_id != self.run_ids[0]
            or self.initial_run_id != self.run_ids[-1]
            or len(set(self.run_ids)) != len(self.run_ids)
        ):
            raise ValueError("验证调用必须属于该独立 Job 自身的恢复链")
        identities = {call.call_id for call in self.model_calls}
        if len(identities) != len(self.model_calls) or any(
            c.run_id not in self.run_ids
            or (c.origin_call_id is not None and c.origin_call_id not in identities)
            for c in self.model_calls
        ):
            raise ValueError("恢复不得借用筛选、缩减或其他验证 Job 的模型调用")
        origins = {call.call_id: call.origin_call_id for call in self.model_calls}
        for call in self.model_calls:
            seen, parent = {call.call_id}, call.origin_call_id
            while parent is not None:
                if parent in seen:
                    raise ValueError("验证模型调用的恢复来源不能成环")
                seen.add(parent)
                parent = origins[parent]
        return self


class RepairValidationReceipt(Contract):
    kind: Literal["repair_validation_receipt/v1"] = "repair_validation_receipt/v1"
    study_id: Identifier
    trial_id: Identifier
    sample_id: Identifier
    phase: Literal["confirm", "regression"]
    job_id: Identifier | None
    initial_run_id: Identifier | None
    current_run_id: Identifier | None
    protocol_ref: Digest
    candidate_ref: Digest
    sample_ref: Digest
    validator_ref: Digest
    projection_ref: Digest
    verdict: Literal["passed", "failed", "incomplete"]
    category: Literal["oracle", "execution", "budget", "cancelled", "safety"]
    usage: RepairTrialUsage
    not_started: bool = False
    replay_report_ref: Digest | None
    provenance_ref: Digest | None
    fresh_model_calls: Annotated[int, Field(ge=0, le=1000)]
    assessment: FrozenAssessment | None = None
    safety_violations: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    uncertainties: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    test_only: Literal[True] = True

    @model_validator(mode="after")
    def actual_evidence(self) -> "RepairValidationReceipt":
        if self.not_started:
            if (
                any(
                    (
                        self.job_id,
                        self.initial_run_id,
                        self.current_run_id,
                        self.replay_report_ref,
                        self.provenance_ref,
                        self.assessment,
                        self.safety_violations,
                        self.fresh_model_calls,
                    )
                )
                or self.category != "cancelled"
                or self.verdict != "incomplete"
                or any(
                    getattr(self.usage, name)
                    for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                )
            ):
                raise ValueError("只有证明未派发的取消意图可以无执行身份且计零用量")
        elif not all(
            (
                self.job_id,
                self.initial_run_id,
                self.current_run_id,
                self.replay_report_ref,
                self.provenance_ref,
            )
        ):
            raise ValueError("验证试验必须绑定自己的独立执行及原始调用来源证明")
        if (self.category == "oracle") != (self.verdict in ("passed", "failed")):
            raise ValueError("只有完整固定硬验收才能报告确定结果")
        if self.category == "oracle" and (
            self.fresh_model_calls == 0
            or self.assessment is None
            or self.assessment.verdict != self.verdict
            or self.assessment.run_id != self.current_run_id
            or self.assessment.validator_ref != self.validator_ref
        ):
            raise ValueError("确认/回归不能用已完成输出或零新模型调用充数")
        if self.fresh_model_calls != self.usage.model_calls:
            raise ValueError("新模型调用来源必须与物理计量逐项一致")
        if self.verdict == "passed" and self.safety_violations:
            raise ValueError("安全违规不能计为确认通过")
        if self.category == "safety" and not self.safety_violations:
            raise ValueError("安全停止必须保留具体违规")
        if self.usage.cost_basis == "recorded_no_external_calls":
            raise ValueError("Recorded 恢复证明不能作为独立确认或回归")
        return self
