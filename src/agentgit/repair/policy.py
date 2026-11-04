"""以已核算的 Mock 字节成本冻结新版默认值；旧 v1 内容保持可读。"""

from typing import Literal

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair import RepairBudget
from agentgit.core.research import AgentConfig, BudgetLimits

MOCK_POLICY = "bounded-templates/mock-bytes-v2"


def repair_model_scope(
    config: AgentConfig, fixture_scope: Literal["stochastic_fixture"] | None = None
) -> Literal["deterministic_fixture", "stochastic_fixture", "provider"]:
    """可保守声明 Mock 的随机范围，不能借此将真实模型变为 fixture。"""
    if config.openai is not None:
        if fixture_scope is not None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "真实模型不能借 Mock fixture 声明改变证据范围")
        return "provider"
    return fixture_scope or "deterministic_fixture"


def mock_policy_version(budget: RepairBudget) -> str:
    return (
        MOCK_POLICY + "@" + object_digest(ObjectType.BLOB, encode(budget.model_dump(mode="json")))
    )


def mock_budget(config: AgentConfig) -> RepairBudget:
    if config.model != "mock-research/v1" or config.openai is not None:
        raise AgentGitError(ErrorCode.FORBIDDEN, "Mock 预算策略不能用于真实供应商模型")
    intended = BudgetLimits(
        max_model_calls=12,
        max_tool_calls=12,
        max_tokens=200_000,
        max_wall_ms=60_000,
        max_cost_micro_usd=0,
    )
    values = intended.model_dump()
    for name in type(intended).model_fields:
        if name in ("schema_version", "max_cost_micro_usd"):
            continue
        values[name] = min(getattr(intended, name), getattr(config.limits, name))
    trial = BudgetLimits.model_validate(values)
    return RepairBudget(
        trial=trial,
        max_model_calls=30 * trial.max_model_calls,
        max_tool_calls=30 * trial.max_tool_calls,
        max_tokens=15 * trial.max_tokens,
    )


def validate_policy(version: str, budget: RepairBudget, config: AgentConfig) -> None:
    if version == "bounded-templates/v1":
        return
    if (
        config.model == "mock-research/v1"
        and config.openai is None
        and version == mock_policy_version(budget)
    ):
        return
    raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "修复策略版本、模型或预算内容不匹配")
