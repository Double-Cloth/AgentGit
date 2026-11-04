"""固定图的调度载荷与外层耐久状态；原生物理记录另存 CAS。"""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from agentgit.core.langgraph_recorded import LangGraphRecordedSpec
from agentgit.core.models import Contract, Digest, Identifier, RunStatus
from agentgit.core.research import BudgetUsage, CallTrace
from agentgit.core.versions import NewBranch


class NewLangGraphFork(NewBranch):
    review_note: Annotated[str, Field(max_length=2048)]


class LangGraphForkSpec(Contract):
    source_run_id: Identifier
    from_commit: Digest
    source_program_ref: Digest
    native_ref: Digest


class LangGraphJobPayload(Contract):
    kind: Literal["langgraph_run"] = "langgraph_run"
    program_ref: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    code_revision: str
    dependency_lock_digest: Digest
    max_wall_ms: Annotated[int, Field(ge=1, le=900_000)]
    fork: LangGraphForkSpec | None = Field(default=None, exclude_if=lambda v: v is None)
    replay: LangGraphRecordedSpec | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def distinct_mode(self) -> "LangGraphJobPayload":
        if self.fork is not None and self.replay is not None:
            raise ValueError("图 Fork 与 Recorded 初始化不能混用")
        return self


class LangGraphRunState(Contract):
    kind: Literal["langgraph_run/v1"] = "langgraph_run/v1"
    program_ref: Digest
    status: RunStatus = RunStatus.RUNNING
    usage: BudgetUsage = BudgetUsage()
    pending: bool = False
    actual_attempts: Annotated[int, Field(ge=0, le=1)] | None = 0
    trace: CallTrace | None = None
    native_ref: Digest | None = None
    native_operation: Literal["checkpoint", "writes"] | None = None
    native_next: tuple[str, ...] = ()
    values: dict[str, Any] = Field(default_factory=dict)
    review_boundary_reached: bool = False
    fork_initialized: bool = False
    replay_control_index: Annotated[int, Field(ge=0, le=64)] = 0
    replay_pause_pending: bool = False
    replay_call_consumed: bool = False
    error_code: str | None = None

    @model_validator(mode="after")
    def bounded_state(self) -> "LangGraphRunState":
        if (
            self.usage.steps > 3
            or self.usage.steps < self.usage.tool_calls
            or self.usage.tool_calls not in (0, 1)
            or self.usage.model_calls != 0
            or self.usage.tokens != 0
            or self.usage.replans != 0
            or self.usage.reserved_tokens != 0
            or self.usage.cost_micro_units != 0
            or self.native_next not in ((), ("__start__",), ("fetch",), ("review",), ("publish",))
            or ((self.native_ref is None) != (self.native_operation is None))
            or (
                self.native_ref is None
                and (self.values or self.native_next or self.review_boundary_reached)
            )
            or (self.pending and self.usage.tool_calls != 1)
            or (self.trace is not None and (self.pending or self.usage.tool_calls != 1))
            or (self.actual_attempts is not None and self.actual_attempts > self.usage.tool_calls)
        ):
            raise ValueError("LangGraph 原生状态或逻辑用量不符合固定范围")
        if self.values and (
            set(self.values)
            != {
                "fetch_ref",
                "fetch_text",
                "review_note",
                "reviewed_text",
                "report",
                "error_code",
                "node_order",
            }
            or any(
                type(self.values[key]) is not str
                for key in ("fetch_text", "review_note", "reviewed_text", "report")
            )
            or self.values["node_order"]
            not in ([], ["fetch"], ["fetch", "review"], ["fetch", "review", "publish"])
            or len(self.values["node_order"]) > self.usage.steps
            or (
                self.values["fetch_ref"] is not None
                and (self.trace is None or self.values["fetch_ref"] != self.trace.response_ref)
            )
        ):
            raise ValueError("LangGraph 值与已完成节点或调用来源不符")
        if self.status == RunStatus.COMPLETED and (
            self.native_ref is None
            or self.native_next
            or self.pending
            or self.trace is None
            or self.error_code is not None
            or self.values.get("error_code") is not None
            or not self.values.get("report")
            or self.values.get("node_order") != ["fetch", "review", "publish"]
        ):
            raise ValueError("LangGraph 完成必须有完整图、成功调用和产物")
        return self

    def logical_projection(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "program_ref": self.program_ref,
            "status": self.status.value,
            "usage": self.usage.model_dump(mode="json"),
            "pending": self.pending,
            "next": list(self.native_next),
            "values": self.values,
            "review_boundary_reached": self.review_boundary_reached,
            "fork_initialized": self.fork_initialized,
            "error_code": self.error_code,
        }
