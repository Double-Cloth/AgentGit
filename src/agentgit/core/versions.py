"""S5 来源路径、Fork 与结构化差异的严格契约。"""

import base64
import binascii
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from agentgit.core.canonical import decode
from agentgit.core.models import Contract, Digest, Identifier, parse_contract
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.research import AgentConfig, BudgetLimits, BudgetUsage, Plan


class NewBranch(Contract):
    source_run_id: Identifier
    from_commit: Digest
    name: Annotated[str, Field(min_length=1, max_length=100)]
    expected_version: Annotated[int, Field(ge=0, le=0)]

    @field_validator("name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("分支名不能包含控制字符或首尾空白")
        return value


class RecordedRange(Contract):
    source_run_id: Identifier
    from_commit: Digest
    until_commit: Digest


class RecordedReplay(RecordedRange):
    mode: Literal["recorded"] = "recorded"
    branch_name: Annotated[str, Field(min_length=1, max_length=100)]
    expected_version: Annotated[int, Field(ge=0, le=0)] = 0

    @field_validator("branch_name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        return NewBranch.visible_name(value)


class RecordedReplaySpec(RecordedRange):
    mode: Literal["recorded"] = "recorded"
    environment_ref: Digest
    checkpoint_count: Annotated[int, Field(ge=1, le=10001)]
    budget_baseline: BudgetUsage | None = None


class ArtifactPatch(Contract):
    path: str
    operation: Literal["write", "delete"] = "write"
    text: str | None = None
    content_b64: str | None = None
    media_type: Annotated[str | None, Field(max_length=200)] = None

    @model_validator(mode="after")
    def valid_patch(self) -> "ArtifactPatch":
        validate_artifact_paths([self.path])
        if self.operation == "delete":
            if self.text is not None or self.content_b64 is not None or self.media_type is not None:
                raise ValueError("删除产物不能附带内容或 MIME")
        elif (self.text is None) == (self.content_b64 is None):
            raise ValueError("写入必须明确选择 UTF-8 文本或 base64 原字节")
        if self.content_b64 is not None:
            try:
                base64.b64decode(self.content_b64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("产物 base64 编码非法") from error
        return self

    def content(self) -> bytes:
        return (
            self.text.encode("utf-8")
            if self.text is not None
            else base64.b64decode(self.content_b64 or "", validate=True)
        )


class ExecutionOverride(Contract):
    tool: Literal["terminal_exec", "code_exec"]
    path: str
    node_key: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    cwd: str = "."
    argv: Annotated[tuple[str, ...], Field(max_length=100)] = ()
    stdin_path: str | None = None
    timeout_ms: Annotated[int, Field(ge=1, le=120000)] = 30000

    @model_validator(mode="after")
    def valid_paths(self) -> "ExecutionOverride":
        for path in (self.path, self.stdin_path, self.cwd if self.cwd != "." else None):
            if path is not None:
                validate_artifact_paths([path])
        if self.tool == "terminal_exec" and self.argv:
            raise ValueError("终端参数应写入版本化命令文件")
        return self


class LiveOverrides(Contract):
    config: AgentConfig | None = None
    plan: Plan | None = None
    artifacts: Annotated[tuple[ArtifactPatch, ...], Field(max_length=1000)] = ()
    execution: ExecutionOverride | None = None

    @model_validator(mode="after")
    def unique_paths(self) -> "LiveOverrides":
        validate_artifact_paths(item.path for item in self.artifacts)
        return self


class LiveReplay(Contract):
    mode: Literal["live"] = "live"
    source_run_id: Identifier
    from_commit: Digest
    branch_name: Annotated[str, Field(min_length=1, max_length=100)]
    expected_version: Annotated[int, Field(ge=0, le=0)] = 0
    budget: BudgetLimits | None = None
    overrides: LiveOverrides = LiveOverrides()

    @field_validator("branch_name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        return NewBranch.visible_name(value)


class LiveReplaySpec(Contract):
    mode: Literal["live"] = "live"
    source_run_id: Identifier
    from_commit: Digest
    environment_ref: Digest
    initial_state_ref: Digest
    budget_baseline: BudgetUsage


def parse_replay(payload: bytes) -> RecordedReplay | LiveReplay:
    value = decode(payload)
    if isinstance(value, dict) and value.get("mode") == "live":
        return parse_contract(payload, LiveReplay)
    return parse_contract(payload, RecordedReplay)


class StructuralChange(Contract):
    path: str
    operation: Literal["add", "remove", "replace"]
    before: Any = None
    after: Any = None
