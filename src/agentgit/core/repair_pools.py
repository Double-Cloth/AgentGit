"""确认阶段预注册调用条件；票据不是已执行样本或统计独立性证明。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.repair_validation import RepairValidationPlan, ValidationSample, validation_ref


class RegressionOrigin(Contract):
    source_run_id: Identifier
    from_commit: Digest


class NewRepairValidationSuite(Contract):
    search_id: Identifier
    regression: Annotated[tuple[RegressionOrigin, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def distinct_origins(self) -> "NewRepairValidationSuite":
        if len({s.source_run_id for s in self.regression}) != 4:
            raise ValueError("四个回归任务必须来自不同运行")
        return self


class OpenRepairValidationPool(Contract):
    suite_id: Identifier


class RepairSamplingTicket(Contract):
    kind: Literal["repair_sampling_ticket/v1"] = "repair_sampling_ticket/v1"
    generation: Literal["fresh_model_invocations/v1"] = "fresh_model_invocations/v1"
    generation_id: Identifier
    source_run_id: Identifier
    from_commit: Digest
    task_ref: Digest
    validator_ref: Digest
    corpus_ref: Digest
    environment_ref: Digest
    source_state_ref: Digest
    source_config_ref: Digest
    model_conditions_ref: Digest
    code_revision: Annotated[str, Field(pattern=r"^source-sha256:[0-9a-f]{64}$")]
    dependency_lock_digest: Digest
    execution_mode: Literal["live", "hybrid"]
    response_status: Literal["not_generated"] = "not_generated"
    candidate_policy: Literal["apply_only_locked_candidate/v1"] = "apply_only_locked_candidate/v1"
    test_only: Literal[True] = True


class RegisteredValidationSample(Contract):
    ticket: RepairSamplingTicket
    sample: ValidationSample

    @model_validator(mode="after")
    def exact_conditions(self) -> "RegisteredValidationSample":
        if (
            self.sample.role == "reduce"
            or self.sample.response_set_ref != validation_ref(self.ticket)
            or self.sample.task_ref != self.ticket.task_ref
            or self.sample.validator_ref != self.ticket.validator_ref
        ):
            raise ValueError("预注册样本必须绑定服务冻结的完整采样条件")
        return self


class RepairValidationSuiteManifest(Contract):
    kind: Literal["repair_validation_suite/v1"] = "repair_validation_suite/v1"
    search_id: Identifier
    source_manifest_ref: Digest
    confirm: Annotated[tuple[RegisteredValidationSample, ...], Field(min_length=6, max_length=6)]
    regression: Annotated[tuple[RegisteredValidationSample, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def frozen_roles(self) -> "RepairValidationSuiteManifest":
        samples = (*self.confirm, *self.regression)
        if (
            any(s.sample.role != "confirm" for s in self.confirm)
            or any(s.sample.role != "regression" for s in self.regression)
            or len({s.ticket.generation_id for s in samples}) != 10
            or len({s.sample.response_set_ref for s in samples}) != 10
            or len({s.sample.task_ref for s in self.confirm}) != 1
            or len({s.sample.task_ref for s in self.regression}) != 4
            or {s.sample.task_ref for s in self.confirm}
            & {s.sample.task_ref for s in self.regression}
        ):
            raise ValueError("六次确认和四个固定回归不能复用票据或任务角色")
        return self


class RepairValidationOpening(Contract):
    kind: Literal["repair_validation_opening/v1"] = "repair_validation_opening/v1"
    suite_id: Identifier
    study_id: Identifier
    suite_ref: Digest
    plan: RepairValidationPlan
    execution_status: Literal["not_started"] = "not_started"
    independence_scope: Literal["registered_fresh_invocations_only"] = (
        "registered_fresh_invocations_only"
    )
    limitations: tuple[str, ...] = (
        "票据只冻结调用条件；未执行的票据不是成功样本，也不证明统计独立性。",
        "后续每项必须绑定新的实际 Job/Run 和原始调用，允许独立生成相同输出，不按输出筛样。",
    )
