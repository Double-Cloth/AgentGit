"""开启确认前检查六次确认和四次回归的完整最坏预留。"""

from agentgit.core.repair import BudgetDecision, RepairBudget, RepairConsumption


def validation_preflight(budget: RepairBudget, usage: RepairConsumption) -> BudgetDecision:
    def result(allowed: bool, reason: str) -> BudgetDecision:
        return BudgetDecision.model_validate(
            dict(allowed=allowed, reason=reason, protected_trials=10)
        )

    if usage.started.confirm or usage.started.regression:
        return result(False, "phase_trials")
    if budget.quotas.confirm < 6 or budget.quotas.regression < 4:
        return result(False, "phase_trials")
    if usage.started.total() + 10 > budget.max_trials:
        return result(False, "total_trials")
    if usage.active:
        return result(False, "concurrency")
    for reason, actual, maximum, cap in (
        ("model_calls", usage.model_calls, budget.trial.max_model_calls, budget.max_model_calls),
        ("tool_calls", usage.tool_calls, budget.trial.max_tool_calls, budget.max_tool_calls),
        ("tokens", usage.tokens, budget.trial.max_tokens, budget.max_tokens),
        (
            "cost",
            usage.cost_micro_usd,
            budget.trial.max_cost_micro_usd or 0,
            budget.max_cost_micro_usd,
        ),
        ("wall_time", usage.elapsed_wall_ms, budget.trial.max_wall_ms, budget.max_wall_ms),
    ):
        if actual + 10 * maximum > cap:
            return result(False, reason)
    return result(True, "available")
