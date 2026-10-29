"""单次 MCP 程序的不可变输入、调度载荷和逻辑状态。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.canonical import encode
from agentgit.core.mcp import MCPArguments, MCPBinding, check_mcp_credentials
from agentgit.core.mcp_schema import validate_value
from agentgit.core.models import Contract, Digest, Identifier, RunStatus
from agentgit.core.research import BudgetLimits, BudgetUsage, CallTrace


class MCPProgram(Contract):
    kind: Literal["mcp_single_call/v1"] = "mcp_single_call/v1"
    binding: MCPBinding
    request: MCPArguments
    limits: BudgetLimits = BudgetLimits(
        max_steps=1,
        max_model_calls=1,
        max_tool_calls=1,
        max_replans=0,
        max_response_bytes=65_536,
        max_artifact_bytes=65_536,
        max_files=1,
    )

    @model_validator(mode="after")
    def bounded_call(self) -> "MCPProgram":
        payload = encode(self.request.model_dump(mode="json"))
        if len(payload) > 1_048_000:
            raise ValueError("MCP 参数超过上限")
        validate_value(self.binding.definition()["inputSchema"], self.request.arguments)
        check_mcp_credentials(self.request.arguments)
        if (
            self.limits.max_tool_calls != 1
            or self.limits.max_files < 1
            or self.binding.server.max_response_bytes > self.limits.max_response_bytes
            or self.binding.server.max_response_bytes > self.limits.max_artifact_bytes
            or self.binding.server.timeout_ms > self.limits.max_wall_ms
        ):
            raise ValueError("MCP 单次程序必须在固定调用、响应及墙钟预算内")
        return self


class MCPRecordedSpec(Contract):
    mode: Literal["recorded"] = "recorded"
    source_run_id: Identifier
    from_commit: Digest
    to_commit: Digest


class MCPJobPayload(Contract):
    kind: Literal["mcp_run"] = "mcp_run"
    program_ref: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    code_revision: str
    dependency_lock_digest: Digest
    max_wall_ms: Annotated[int, Field(ge=1, le=900_000)]
    replay: MCPRecordedSpec | None = None


class MCPRunState(Contract):
    kind: Literal["mcp_run/v1"] = "mcp_run/v1"
    program_ref: Digest
    status: RunStatus = RunStatus.RUNNING
    usage: BudgetUsage = BudgetUsage()
    pending: bool = False
    actual_attempts: Annotated[int, Field(ge=0, le=1)] | None = 0
    response_ref: Digest | None = None
    error_code: str | None = None
    trace: CallTrace | None = None

    @model_validator(mode="after")
    def single_call_state(self) -> "MCPRunState":
        if (
            self.usage.steps not in (0, 1)
            or self.usage.tool_calls not in (0, 1)
            or self.usage.steps != self.usage.tool_calls
            or self.usage.model_calls != 0
            or self.usage.tokens != 0
            or self.usage.cost_micro_units != 0
            or self.usage.reserved_tokens != 0
            or self.usage.replans != 0
            or (self.pending and self.usage.tool_calls != 1)
            or (self.actual_attempts is not None and self.actual_attempts > self.usage.tool_calls)
        ):
            raise ValueError("MCP 单次程序的逻辑用量或实际尝试不符")
        if self.response_ref is not None and (
            self.trace is None
            or self.trace.response_ref != self.response_ref
            or self.pending
            or self.usage.tool_calls != 1
        ):
            raise ValueError("MCP 结果必须绑定完整调用轨迹")
        if self.status == RunStatus.COMPLETED and (
            self.response_ref is None or self.error_code is not None
        ):
            raise ValueError("MCP 完成状态必须有确切成功结果")
        return self

    def logical_projection(self) -> dict[str, object]:
        # 新 Run 身份和本次实际尝试不属于 Recorded 的逻辑结果投影。
        return {
            "kind": self.kind,
            "program_ref": self.program_ref,
            "status": self.status.value,
            "usage": self.usage.model_dump(mode="json"),
            "pending": self.pending,
            "response_ref": self.response_ref,
            "error_code": self.error_code,
        }
