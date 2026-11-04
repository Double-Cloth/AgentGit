"""独立合并分支和新 Run 输入；不借用旧 Run 身份或执行授权。"""

from typing import Annotated

from pydantic import Field, field_validator

from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.research import BudgetLimits
from agentgit.core.versions import NewBranch


class ForkMergedBranch(Contract):
    approval_id: Identifier
    commit_id: Digest
    name: Annotated[str, Field(min_length=1, max_length=100)]

    @field_validator("name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        return NewBranch.visible_name(value)


class NewMergedRun(Contract):
    branch_name: Annotated[str, Field(min_length=1, max_length=100)]
    expected_head: Digest
    expected_branch_version: Annotated[int, Field(ge=0)]
    config_id: Identifier
    config_version: Annotated[int, Field(ge=1)]
    validator_ref: Digest
    budget: BudgetLimits

    @field_validator("branch_name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        return NewBranch.visible_name(value)


class MergedRunSpec(Contract):
    approval_id: Identifier
    from_commit: Digest
    initial_state_ref: Digest
    validator_ref: Digest
