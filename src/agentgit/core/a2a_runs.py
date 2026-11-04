"""A2A 有界程序、调度载荷与可恢复状态；远端引用只从本 Run 回执派生。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.a2a import A2ABinding
from agentgit.core.a2a_wire import STATES, A2ACall
from agentgit.core.models import Contract, Digest, Identifier, RunStatus
from agentgit.core.research import BudgetLimits, BudgetUsage, CallTrace


class A2AProgram(Contract):
    kind: Literal["fixed_a2a_task/v1"] = "fixed_a2a_task/v1"
    binding: A2ABinding
    messages: Annotated[tuple[str, ...], Field(min_length=1, max_length=4)]
    max_polls: Annotated[int, Field(ge=1, le=32)] = 16
    poll_interval_ms: Annotated[int, Field(ge=20, le=1000)] = 100
    limits: BudgetLimits = BudgetLimits(
        max_steps=40,
        max_tool_calls=40,
        max_model_calls=1,
        max_replans=0,
        max_response_bytes=65_536,
        max_artifact_bytes=262_144,
        max_files=9,
    )

    @model_validator(mode="after")
    def bounded_program(self) -> "A2AProgram":
        identifiers = []
        for raw in self.messages:
            call = A2ACall(operation="send", body_json=raw)
            identifiers.append(call.body()["message"]["messageId"])
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("A2A 预声明消息 ID 不能重复")
        if (
            not len(self.messages) + self.max_polls + 1 <= self.limits.max_tool_calls <= 64
            or self.limits.max_steps != self.limits.max_tool_calls
            or self.binding.server.max_response_bytes > self.limits.max_response_bytes
            or self.binding.server.max_response_bytes * 2 > self.limits.max_artifact_bytes
            or self.binding.server.timeout_ms > self.limits.max_wall_ms
            or self.poll_interval_ms > self.limits.max_wall_ms
            or self.limits.max_files < 9
        ):
            raise ValueError("A2A 程序必须声明消息、轮询、取消、响应及产物的有界预算")
        return self


class A2ARecordedSpec(Contract):
    mode: Literal["recorded"] = "recorded"
    source_run_id: Identifier
    from_commit: Digest
    to_commit: Digest


class NewA2ARun(Contract):
    program: A2AProgram
    branch_name: Annotated[str, Field(min_length=1, max_length=100)] = "main"
    expected_head: Digest | None
    expected_version: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    replay: A2ARecordedSpec | None = None


class A2AJobPayload(Contract):
    kind: Literal["a2a_run"] = "a2a_run"
    program_ref: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    code_revision: Annotated[str, Field(pattern=r"^source-sha256:[0-9a-f]{64}$")]
    dependency_lock_digest: Digest
    max_wall_ms: Annotated[int, Field(ge=1, le=900_000)]
    replay: A2ARecordedSpec | None = Field(default=None, exclude_if=lambda v: v is None)


class A2AObservation(Contract):
    call: A2ACall
    trace: CallTrace
    error_code: str | None = None


class A2ARetiredCall(Contract):
    step: Annotated[str, Field(pattern=r"^a2a-[0-9]{4}$")]
    call: A2ACall


class A2ARunState(Contract):
    kind: Literal["a2a_run/v1"] = "a2a_run/v1"
    program_ref: Digest
    status: RunStatus = RunStatus.RUNNING
    usage: BudgetUsage = BudgetUsage()
    observations: Annotated[tuple[A2AObservation, ...], Field(max_length=64)] = ()
    retired: Annotated[tuple[A2ARetiredCall, ...], Field(max_length=64)] = ()
    pending: A2ACall | None = None
    defer_pending: bool = False
    next_message: Annotated[int, Field(ge=0, le=4)] = 0
    polls: Annotated[int, Field(ge=0, le=32)] = 0
    last_response_ref: Digest | None = None
    remote_state: str | None = None
    waiting_for: Literal["input", "auth"] | None = None
    cancel_requested: bool = False
    remote_cancel_status: Literal[
        "not_requested",
        "not_sent",
        "pending_approval",
        "confirmed",
        "rejected",
        "not_confirmed",
        "outcome_unknown",
    ] = "not_requested"
    error_code: str | None = None

    @property
    def pending_step(self) -> str:
        return f"a2a-{self.usage.steps:04d}"

    @model_validator(mode="after")
    def consistent_ledger(self) -> "A2ARunState":
        calls = [o.call for o in self.observations] + [o.call for o in self.retired]
        if self.pending is not None:
            calls.append(self.pending)
        if (
            self.usage.steps != self.usage.tool_calls
            or self.usage.tool_calls != len(calls)
            or self.usage.tool_calls > 64
            or self.usage.model_calls != 0
            or self.usage.tokens != 0
            or self.usage.replans != 0
            or self.usage.reserved_tokens != 0
            or self.usage.cost_micro_units != 0
            or self.polls != sum(c.operation == "get" for c in calls)
            or self.next_message
            != sum(o.call.operation == "send" and o.error_code is None for o in self.observations)
        ):
            raise ValueError("A2A 逻辑预算、消息进度或轮询账本不一致")
        steps = [o.trace.logical_step_id for o in self.observations] + [
            o.step for o in self.retired
        ]
        if self.pending is not None:
            steps.append(self.pending_step)
        if len(set(steps)) != len(steps) or set(steps) != {
            f"a2a-{i:04d}" for i in range(1, len(calls) + 1)
        }:
            raise ValueError("A2A 调用顺序不连续或重复")
        successful = [o for o in self.observations if o.error_code is None]
        if self.last_response_ref != (successful[-1].trace.response_ref if successful else None):
            raise ValueError("A2A 最后回执与已确认调用不符")
        if self.status == RunStatus.COMPLETED and (
            self.remote_state != "TASK_STATE_COMPLETED"
            or self.last_response_ref is None
            or self.pending is not None
            or self.error_code is not None
        ):
            raise ValueError("A2A 完成状态必须有确切远端完成回执")
        if self.remote_state is not None and self.remote_state not in STATES:
            raise ValueError("A2A 远端状态不在已固定协议范围")
        if self.defer_pending and (self.pending is not None or self.status != RunStatus.RUNNING):
            raise ValueError("A2A 调度意图不能与未决调用或停止状态混同")
        if any(o.trace.error_code != o.error_code for o in self.observations):
            raise ValueError("A2A 观察错误与确切调用轨迹不符")
        if self.remote_cancel_status == "confirmed" and self.remote_state != "TASK_STATE_CANCELED":
            raise ValueError("A2A 不能将未确认的取消记作已取消")
        return self

    def logical_projection(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class A2ARecordedRunState(Contract):
    kind: Literal["a2a_recorded_run/v1"] = "a2a_recorded_run/v1"
    program_ref: Digest
    replay: A2ARecordedSpec
    cursor: Annotated[int, Field(ge=0, le=128)]
    status: RunStatus = RunStatus.RUNNING
    logical_state: A2ARunState
    actual_attempts: Literal[0] = 0
    actual_response_bytes: Literal[0] = 0
    error_code: str | None = None

    @model_validator(mode="after")
    def local_progress(self) -> "A2ARecordedRunState":
        if (
            self.program_ref != self.logical_state.program_ref
            or self.cursor
            != 2 * len(self.logical_state.observations) + (self.logical_state.pending is not None)
            or self.logical_state.retired
            or self.status
            not in {
                RunStatus.RUNNING,
                RunStatus.PAUSED,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.COMPLETED,
                RunStatus.BUDGET_EXHAUSTED,
            }
            or (
                self.status == RunStatus.COMPLETED
                and self.logical_state.status != RunStatus.COMPLETED
            )
        ):
            raise ValueError("A2A Recorded 本地进度与完整历史逻辑状态不符")
        return self

    def logical_projection(self) -> dict[str, object]:
        return self.logical_state.logical_projection()
