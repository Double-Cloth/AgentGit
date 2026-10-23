"""模型调用的纯预算转移；Runtime、接管封存和 Recorded 共用，不访问网络。"""

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.model_usage import ModelMeter, ModelTotals, price_micro_usd
from agentgit.core.models import ModelCallBudget
from agentgit.core.research import AgentConfig, BudgetUsage, evolve


class ModelResultError(AgentGitError):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool):
        super().__init__(code, message)
        self.retryable = retryable


def reservation(config: AgentConfig, request_bytes: int) -> tuple[int, int]:
    options = config.openai
    if options is None:
        return request_bytes + config.limits.max_response_bytes, 0
    return (
        options.max_input_tokens + options.max_output_tokens,
        price_micro_usd(options.max_input_tokens, options.max_output_tokens),
    )


def reserve_model(usage: BudgetUsage, config: AgentConfig, request_bytes: int) -> BudgetUsage:
    tokens, cost = reservation(config, request_bytes)
    changes: dict[str, object] = {}
    if config.openai is not None:
        totals = usage.model_meter or ModelTotals()
        changes["model_meter"] = evolve(
            totals, reserved_cost_micro_usd=totals.reserved_cost_micro_usd + cost
        )
        changes["usage_kind"] = (
            "mixed_units" if usage.tokens > totals.accounted_tokens else "provider_tokens"
        )
    elif usage.model_meter is not None:
        changes["usage_kind"] = "mixed_units"
    return evolve(
        usage,
        steps=usage.steps + 1,
        model_calls=usage.model_calls + 1,
        reserved_tokens=tokens,
        **changes,
    )


def call_budget(config: AgentConfig) -> ModelCallBudget | None:
    if config.openai is None:
        return None
    tokens, cost = reservation(config, 0)
    return ModelCallBudget(tokens=tokens, cost_micro_usd=cost, pricebook=config.openai.pricebook)


def settle_model(usage: BudgetUsage, meter: ModelMeter, *, retry_delay_ms: int = 0) -> BudgetUsage:
    totals = usage.model_meter or ModelTotals()
    accounted = meter.accounted_input_tokens + meter.accounted_output_tokens
    measured = meter.basis == "provider_usage"
    updated = evolve(
        totals,
        measured_calls=totals.measured_calls + int(measured),
        estimated_calls=totals.estimated_calls + int(meter.basis == "conservative_bound"),
        not_generated_calls=totals.not_generated_calls + int(meter.basis == "not_generated"),
        measured_input_tokens=totals.measured_input_tokens + (meter.input_tokens or 0),
        measured_output_tokens=totals.measured_output_tokens + (meter.output_tokens or 0),
        cached_tokens=totals.cached_tokens + (meter.cached_tokens or 0),
        reasoning_tokens=totals.reasoning_tokens + (meter.reasoning_tokens or 0),
        missing_cache_detail_calls=totals.missing_cache_detail_calls
        + int(measured and meter.cached_tokens is None),
        missing_reasoning_detail_calls=totals.missing_reasoning_detail_calls
        + int(measured and meter.reasoning_tokens is None),
        accounted_tokens=totals.accounted_tokens + accounted,
        reserved_cost_micro_usd=totals.unpriced_cost_micro_usd
        if meter.price_verified
        else totals.reserved_cost_micro_usd,
        unpriced_cost_micro_usd=totals.unpriced_cost_micro_usd
        if meter.price_verified
        else totals.reserved_cost_micro_usd,
        unpriced_calls=totals.unpriced_calls + int(not meter.price_verified),
        next_retry_delay_ms=retry_delay_ms,
    )
    mock_bytes = usage.tokens - totals.accounted_tokens
    kind = "mixed_units" if mock_bytes else "provider_tokens"
    return evolve(
        usage,
        model_meter=updated,
        usage_kind=kind,
        reserved_tokens=0,
        tokens=usage.tokens + accounted,
        cost_micro_units=usage.cost_micro_units
        + (meter.estimated_cost_micro_usd if meter.price_verified else 0),
    )


def clear_reservation(usage: BudgetUsage) -> BudgetUsage:
    totals = usage.model_meter
    if totals is not None:
        totals = evolve(totals, reserved_cost_micro_usd=totals.unpriced_cost_micro_usd)
    return evolve(usage, reserved_tokens=0, model_meter=totals)


def mark_unpriced(usage: BudgetUsage) -> BudgetUsage:
    assert usage.model_meter is not None
    totals = usage.model_meter
    return evolve(
        usage,
        model_meter=evolve(
            totals,
            unpriced_calls=totals.unpriced_calls + 1,
            unpriced_cost_micro_usd=totals.reserved_cost_micro_usd,
        ),
    )
