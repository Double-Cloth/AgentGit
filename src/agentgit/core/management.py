"""HTTP 与 CLI 共用的严格输入契约。"""

from typing import Annotated, Literal

from pydantic import Field

from agentgit.core.models import Contract, Digest, Identifier, NonNegative
from agentgit.core.research import AgentConfig, BudgetLimits, ResearchGoal


class NewRepository(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    expected_version: Annotated[int, Field(ge=0, le=0)]


class NewAgent(Contract):
    config: AgentConfig = AgentConfig()
    expected_version: NonNegative


class NewTask(Contract):
    config_id: Identifier
    config_version: Annotated[int, Field(ge=1)]
    goal: ResearchGoal
    expected_version: Annotated[int, Field(ge=0, le=0)]


class SubmitRun(Contract):
    budget: BudgetLimits | None = None
    config_version: Annotated[int, Field(ge=1)]
    expected_branch_version: NonNegative
    expected_head: Digest | None
    branch_name: Annotated[str, Field(min_length=1, max_length=100)] = "main"


class ControlRun(Contract):
    expected_version: NonNegative


class DecideApproval(Contract):
    expected_version: NonNegative
    decision: Literal["approve", "reject"]
