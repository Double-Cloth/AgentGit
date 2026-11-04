"""S10 持久缩减绑定与物理试验收据；确认样本的开启另由后续阶段控制。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.experiments import FrozenAssessment
from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.repair import RepairTrialUsage
from agentgit.core.repair_validation import DeletionRequest, ReductionState
from agentgit.core.research import BudgetLimits


class NewRepairStudy(Contract):
    search_id: Identifier
    max_reduce_trials: Annotated[int, Field(ge=0, le=100)] | None = None
    field_policy: Literal["predefined-fields/v1"] | None = Field(
        default="predefined-fields/v1", exclude_if=lambda value: value is None
    )


class RepairStudyManifest(Contract):
    kind: Literal["repair_reduction_study/v1"] = "repair_reduction_study/v1"
    search_id: Identifier
    source_manifest_ref: Digest
    source_decision_ref: Digest
    selected_proposal_id: Identifier
    selected_trial_id: Identifier
    selected_result_ref: Digest
    initial_state: ReductionState
    code_revision: Annotated[str, Field(pattern=r"^source-sha256:[0-9a-f]{64}$")]
    field_policy: Literal["predefined-fields/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RepairStudyLease(Contract):
    repository_id: Identifier
    study_id: Identifier
    token: Identifier
    worker: Annotated[str, Field(min_length=1, max_length=200)]
    expires_at: datetime


class RepairReductionIntent(Contract):
    kind: Literal["repair_reduction_intent/v1"] = "repair_reduction_intent/v1"
    study_id: Identifier
    ordinal: Annotated[int, Field(ge=0, le=99)]
    phase: Literal["reduce"] = "reduce"
    protocol_ref: Digest
    deletion: DeletionRequest
    branch_name: Annotated[str, Field(pattern=r"^reduce-[0-9a-f]{32}-[0-9]{1,2}$")]
    limits: BudgetLimits
    hybrid_ref: Digest | None = None


class RepairReductionReceipt(Contract):
    kind: Literal["repair_reduction_receipt/v1"] = "repair_reduction_receipt/v1"
    study_id: Identifier
    trial_id: Identifier
    job_id: Identifier | None
    initial_run_id: Identifier | None
    current_run_id: Identifier | None
    protocol_ref: Digest
    candidate_ref: Digest
    sample_ref: Digest
    validator_ref: Digest
    verdict: Literal["passed", "failed", "incomplete"]
    category: Literal["oracle", "execution", "budget", "cancelled", "safety"]
    usage: RepairTrialUsage
    not_started: bool = False
    replay_report_ref: Digest | None
    assessment: FrozenAssessment | None = None
    safety_violations: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    uncertainties: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    test_only: Literal[True] = True

    @model_validator(mode="after")
    def physical_binding(self) -> "RepairReductionReceipt":
        if self.not_started:
            if (
                any(
                    (
                        self.job_id,
                        self.initial_run_id,
                        self.current_run_id,
                        self.replay_report_ref,
                        self.assessment,
                        self.safety_violations,
                    )
                )
                or self.category != "cancelled"
                or self.verdict != "incomplete"
            ):
                raise ValueError("未启动意图只能按确定未派发的取消结算")
            if any(
                getattr(self.usage, name)
                for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
            ):
                raise ValueError("确定未启动的意图不能记入物理调用")
        elif not all(
            (self.job_id, self.initial_run_id, self.current_run_id, self.replay_report_ref)
        ):
            raise ValueError("实际试验必须绑定独立 Job、初始/当前 Run 和 Replay 报告")
        if (self.category == "oracle") != (self.verdict in ("passed", "failed")):
            raise ValueError("非固定硬验收结果必须保留为不完整")
        if self.category == "oracle" and (
            self.assessment is None
            or self.assessment.verdict != self.verdict
            or self.assessment.run_id != self.current_run_id
            or self.assessment.validator_ref != self.validator_ref
        ):
            raise ValueError("缩减结论必须绑定实际当前 Run 的固定验收")
        if self.verdict == "passed" and self.safety_violations:
            raise ValueError("安全违规不能作为删除通过证据")
        if self.category == "safety" and not self.safety_violations:
            raise ValueError("安全停止必须保留具体违规证据")
        if self.usage.cost_basis == "recorded_no_external_calls":
            raise ValueError("Recorded 恢复证明不能作为独立缩减试验")
        return self
