"""新版 Mock 默认值绑定实际字节成本，不能扩大来源授权或冒充供应商预算。"""

import pytest

from agentgit.core.errors import AgentGitError
from agentgit.core.repair import RepairBudget, RepairConsumption
from agentgit.core.research import AgentConfig, BudgetLimits, evolve
from agentgit.repair.budget import admission
from agentgit.repair.changes import change_span
from agentgit.repair.policy import mock_budget, mock_policy_version, validate_policy


def test_new_mock_policy_covers_measured_trial_and_preserves_future_confirmation():
    budget = mock_budget(AgentConfig())
    assert budget.trial.max_model_calls == 12 and budget.trial.max_tokens == 200_000
    assert budget.max_tokens == 3_000_000
    assert budget.trial.max_cost_micro_usd == budget.max_cost_micro_usd == 0
    assert admission(budget, RepairConsumption(), "screen").allowed
    # 历史实测正常完整路径为 11 次模型、9 次工具和 94,103 字节。
    assert budget.trial.max_model_calls >= 11 and budget.trial.max_tool_calls >= 9
    assert budget.trial.max_tokens >= 94_103
    assert budget.max_tokens - 10 * budget.trial.max_tokens == 1_000_000
    version = mock_policy_version(budget)
    assert len(version) <= 100 and version.startswith("bounded-templates/mock-bytes-v2@")
    validate_policy(version, budget, AgentConfig())
    assert mock_policy_version(evolve(budget, max_tokens=2_900_000)) != version
    with pytest.raises(AgentGitError):
        validate_policy(version, evolve(budget, max_tokens=2_900_000), AgentConfig())


def test_new_policy_never_enlarges_small_source_limits_and_keeps_old_v1():
    config = AgentConfig(limits=BudgetLimits(max_model_calls=2, max_tokens=1000, max_tool_calls=0))
    budget = mock_budget(config)
    assert budget.trial.within(config.limits)
    assert budget.max_tokens == 15_000
    assert RepairBudget().trial.max_tokens == 4000
    validate_policy("bounded-templates/v1", RepairBudget(), config)
    with pytest.raises(AgentGitError):
        validate_policy("bounded-templates/future", budget, config)


@pytest.mark.parametrize(
    "before,after,expected",
    [
        (b"", b"", 0),
        (b"same", b"same", 0),
        (b"abc", b"abXXc", 2),
        (b"abXXc", b"abc", 2),
        (b"aba", b"aaa", 2),
        (b"ab", b"ba", 4),
        (b"aaa", b"a", 2),
        ("中".encode(), "中文".encode(), 3),
    ],
)
def test_change_span_has_nonoverlapping_prefix_and_suffix(before, after, expected):
    assert change_span(before, after) == expected
