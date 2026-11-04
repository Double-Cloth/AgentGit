"""确认/回归的多维预留、未知试验保守占用及上限边界。"""

import pytest
from pydantic import ValidationError

from agentgit.core.repair import PhaseQuotas, RepairBudget, RepairConsumption
from agentgit.core.research import evolve
from agentgit.repair.budget import admission


def test_defaults_and_all_phase_reservations():
    budget = RepairBudget()
    assert budget.quotas.total() == budget.max_trials == 30
    assert (
        budget.trial.max_model_calls,
        budget.trial.max_tool_calls,
        budget.trial.max_tokens,
        budget.trial.max_wall_ms,
    ) == (6, 12, 4000, 60000)
    usage = RepairConsumption()
    assert admission(budget, usage, "screen").protected_trials == 10
    assert admission(budget, usage, "reduce").protected_trials == 10
    assert admission(budget, usage, "confirm").protected_trials == 4
    assert admission(budget, usage, "regression").protected_trials == 0
    assert admission(budget, usage, "screen").allowed


@pytest.mark.parametrize(
    "field,cap,reason",
    [
        ("max_model_calls", 65, "model_calls"),
        ("max_tool_calls", 131, "tool_calls"),
        ("max_tokens", 43999, "tokens"),
        ("max_wall_ms", 659999, "wall_time"),
    ],
)
def test_screen_never_spends_independent_confirmation_reserves(field, cap, reason):
    budget = evolve(RepairBudget(), **{field: cap})
    decision = admission(budget, RepairConsumption(), "screen")
    assert not decision.allowed and decision.reason == reason


@pytest.mark.parametrize(
    "field,amount,reason",
    [
        ("model_calls", 115, "model_calls"),
        ("tool_calls", 229, "tool_calls"),
        ("tokens", 16001, "tokens"),
        ("elapsed_wall_ms", 240001, "wall_time"),
    ],
)
def test_settled_consumption_cannot_use_future_reserves(field, amount, reason):
    usage = evolve(RepairConsumption(), **{field: amount})
    decision = admission(RepairBudget(), usage, "screen")
    assert not decision.allowed and decision.reason == reason


def test_exact_reserved_boundary_allows_one_then_blocks_following_trial():
    budget = RepairBudget()
    usage = RepairConsumption(tokens=16000)
    assert admission(budget, usage, "screen").allowed
    assert not admission(budget, evolve(usage, tokens=20000), "screen").allowed
    # 按实际量结算后可释放差额；未知计量不应冒充这一状态。
    assert admission(budget, evolve(usage, tokens=16001), "confirm").allowed


def test_unknown_trial_occupies_concurrency_and_complete_upper_bound():
    usage = RepairConsumption(
        started=PhaseQuotas(screen=1, reduce=0, confirm=0, regression=0), active=1, tokens=12001
    )
    assert admission(RepairBudget(), usage, "screen").reason == "concurrency"
    budget = evolve(RepairBudget(), max_concurrency=2)
    assert admission(budget, usage, "screen").reason == "tokens"
    assert admission(budget, evolve(usage, tokens=12000), "screen").allowed


def test_phase_quota_is_not_borrowed_and_total_quota_is_hard_limit():
    usage = RepairConsumption(started=PhaseQuotas(screen=14, reduce=0, confirm=0, regression=0))
    assert admission(RepairBudget(), usage, "screen").reason == "phase_trials"
    assert admission(RepairBudget(), usage, "reduce").allowed
    usage = evolve(usage, started=PhaseQuotas())
    assert admission(RepairBudget(), usage, "regression").reason == "total_trials"


def test_fee_limit_is_explicit_and_reserved_in_integer_usd_units():
    budget = RepairBudget(
        max_cost_micro_usd=100,
        pricebook="fixed-test-prices/v1",
        trial=evolve(RepairBudget().trial, max_cost_micro_usd=10),
    )
    decision = admission(budget, RepairConsumption(), "screen")
    assert not decision.allowed and decision.reason == "cost"
    assert admission(evolve(budget, max_cost_micro_usd=110), RepairConsumption(), "screen").allowed


@pytest.mark.parametrize(
    "changes",
    [
        {"trial": evolve(RepairBudget().trial, max_cost_micro_usd=None)},
        {"max_trials": 29},
        {"max_concurrency": 0},
        {"max_cost_micro_usd": 1},
        {"pricebook": "unexpected-for-zero-cost"},
        {"trial": evolve(RepairBudget().trial, max_cost_micro_usd=1)},
    ],
)
def test_unbounded_or_incoherent_budget_is_rejected(changes):
    with pytest.raises(ValidationError):
        evolve(RepairBudget(), **changes)
