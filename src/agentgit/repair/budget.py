"""纯预算判定；持久调度须在同一事务内保存检查结果与完整预留。"""

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair import BudgetDecision, RepairBudget, RepairConsumption, RepairPhase


def protected_trials(budget: RepairBudget, phase: RepairPhase) -> int:
    if phase in ("screen", "reduce"):
        return budget.quotas.confirm + budget.quotas.regression
    if phase == "confirm":
        return budget.quotas.regression
    return 0


def admission(
    budget: RepairBudget,
    usage: RepairConsumption,
    phase: RepairPhase,
) -> BudgetDecision:
    if phase not in ("screen", "reduce", "confirm", "regression"):
        raise AgentGitError(ErrorCode.VALIDATION, "未知修复试验阶段")
    protected = protected_trials(budget, phase)

    def decision(allowed: bool, reason: str) -> BudgetDecision:
        return BudgetDecision.model_validate(
            {
                "allowed": allowed,
                "reason": reason,
                "protected_trials": protected,
            }
        )

    if usage.started.total() >= budget.max_trials:
        return decision(False, "total_trials")
    if getattr(usage.started, phase) >= getattr(budget.quotas, phase):
        return decision(False, "phase_trials")
    if usage.active >= budget.max_concurrency:
        return decision(False, "concurrency")
    # 已结算实际量 + 未决完整上限 + 本次完整上限 + 后续确认/回归预留。
    reservations = usage.active + 1 + protected
    trial = budget.trial
    for reason, consumed, per_trial, cap in (
        ("model_calls", usage.model_calls, trial.max_model_calls, budget.max_model_calls),
        ("tool_calls", usage.tool_calls, trial.max_tool_calls, budget.max_tool_calls),
        ("tokens", usage.tokens, trial.max_tokens, budget.max_tokens),
        ("cost", usage.cost_micro_usd, trial.max_cost_micro_usd or 0, budget.max_cost_micro_usd),
        ("wall_time", usage.elapsed_wall_ms, trial.max_wall_ms, budget.max_wall_ms),
    ):
        if consumed + reservations * per_trial > cap:
            return decision(False, reason)
    return decision(True, "available")
