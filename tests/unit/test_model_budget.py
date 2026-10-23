"""真实模型预算与 Live 混合历史的纯状态边界。"""

import pytest
from pydantic import ValidationError

from agentgit.core.model_usage import ModelMeter, OpenAIOptions
from agentgit.core.research import AgentConfig, BudgetLimits, BudgetUsage, evolve
from agentgit.runtime.model_budget import (
    call_budget,
    clear_reservation,
    mark_unpriced,
    reserve_model,
    settle_model,
)


def config(**limits):
    return AgentConfig(
        model="openai/gpt-5.4-mini-2026-03-17",
        openai=OpenAIOptions(),
        limits=BudgetLimits(max_cost_micro_usd=1_000_000, **limits),
    )


def meter(*, verified=True, basis="provider_usage"):
    measured = basis == "provider_usage"
    return ModelMeter(
        basis=basis,
        counted_input_tokens=100,
        input_tokens=100 if measured else None,
        output_tokens=20 if measured else None,
        cached_tokens=40 if measured else None,
        cache_write_tokens=0 if measured else None,
        reasoning_tokens=5 if measured else None,
        price_verified=verified,
        accounted_input_tokens=100,
        accounted_output_tokens=20,
        estimated_cost_micro_usd=138 if measured else 165,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"openai": OpenAIOptions()},
        {"model": "openai/gpt-5.4-mini-2026-03-17"},
        {"model": "openai/gpt-5.4-mini-2026-03-17", "openai": OpenAIOptions()},
        {
            "model": "openai/gpt-5.4-mini-2026-03-17",
            "openai": OpenAIOptions(),
            "limits": BudgetLimits(max_cost_micro_usd=0),
        },
    ],
)
def test_provider_configuration_requires_explicit_options_and_money(changes):
    with pytest.raises(ValidationError):
        AgentConfig(**changes)


def test_nullable_money_limit_cannot_remove_finite_task_cap():
    cap = BudgetLimits(max_cost_micro_usd=100)
    assert not BudgetLimits().within(cap)
    assert not BudgetLimits(max_cost_micro_usd=101).within(cap)
    assert BudgetLimits(max_cost_micro_usd=0).within(cap)
    assert BudgetLimits(max_cost_micro_usd=99).within(cap, BudgetLimits())
    assert BudgetLimits().within(BudgetLimits())


def test_reservation_is_model_tokens_and_integer_usd_not_request_bytes():
    usage = reserve_model(BudgetUsage(), config(), 999_999)
    assert usage.reserved_tokens == 40_960 and usage.usage_kind == "provider_tokens"
    assert usage.model_meter.reserved_cost_micro_usd == 61_440
    assert call_budget(config()).cost_micro_usd == 61_440
    assert call_budget(AgentConfig()) is None


def test_settlement_counts_output_including_reasoning_once_and_releases_reserve():
    usage = settle_model(reserve_model(BudgetUsage(), config(), 20), meter())
    assert usage.tokens == usage.model_meter.accounted_tokens == 120
    assert usage.cost_micro_units == 138 and usage.reserved_tokens == 0
    assert usage.model_meter.measured_output_tokens == 20
    assert usage.model_meter.reasoning_tokens == 5
    assert usage.model_meter.reserved_cost_micro_usd == 0


def test_unverified_price_is_held_separately_from_spent_and_normal_clear():
    usage = settle_model(reserve_model(BudgetUsage(), config(), 20), meter(verified=False))
    assert usage.cost_micro_units == 0 and usage.tokens == 120
    assert usage.model_meter.unpriced_calls == 1
    assert clear_reservation(usage).model_meter.reserved_cost_micro_usd == 61_440
    assert usage.model_meter.unpriced_cost_micro_usd == 61_440


def test_corrupt_meter_cannot_release_unknown_fees():
    usage = mark_unpriced(reserve_model(BudgetUsage(), config(), 20))
    assert clear_reservation(usage).model_meter.reserved_cost_micro_usd == 61_440
    assert clear_reservation(usage).reserved_tokens == 0


def test_live_legacy_bytes_and_provider_tokens_keep_distinct_accounting():
    baseline = BudgetUsage(tokens=500, model_calls=10, steps=10)
    usage = settle_model(reserve_model(baseline, config(), 20), meter())
    assert usage.tokens == 620 and usage.usage_kind == "mixed_units"
    assert usage.tokens - baseline.tokens == 120
    assert usage.model_meter.accounted_tokens == 120
    mock = reserve_model(usage, AgentConfig(), 100)
    assert mock.usage_kind == "mixed_units" and mock.reserved_tokens == 16_484


def test_parent_unpriced_counter_does_not_hold_new_live_attempt_reservation():
    historical = settle_model(reserve_model(BudgetUsage(), config(), 20), meter(verified=False))
    copied = evolve(
        historical,
        model_meter=evolve(
            historical.model_meter,
            reserved_cost_micro_usd=0,
            unpriced_cost_micro_usd=0,
        ),
    )
    current = reserve_model(copied, config(), 20)
    assert clear_reservation(current).model_meter.reserved_cost_micro_usd == 0
    assert current.model_meter.unpriced_calls == 1
