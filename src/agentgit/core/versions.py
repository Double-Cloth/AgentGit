"""S5 来源路径、Fork 与结构化差异的严格契约。"""

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from agentgit.core.models import Contract, Digest, Identifier


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


class StructuralChange(Contract):
    path: str
    operation: Literal["add", "remove", "replace"]
    before: Any = None
    after: Any = None
