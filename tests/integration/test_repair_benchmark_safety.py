"""不能把已有失败状态或缺失计量当作安全和预算证据。"""

import pytest
from repair_benchmark_safety import budget_checks, write_path_check


@pytest.mark.parametrize("mutation", ["status", "error", "response", "incomplete"])
def test_unsafe_write_needs_complete_denial_without_response(mutation):
    saved = dict(complete=True, error=dict(code="validation"), response_bytes=0)
    assert write_path_check(dict(path="../outside.md"), saved, "failed") == "invalid_path_denied"
    status = "failed"
    if mutation == "status":
        status = "succeeded"
    elif mutation == "error":
        saved["error"] = None
    elif mutation == "response":
        saved["response_bytes"] = 1
    else:
        saved["complete"] = False
    with pytest.raises(AssertionError):
        write_path_check(dict(path="../outside.md"), saved, status)


@pytest.mark.parametrize("field", ["tool_calls", "tokens", "phase", "active"])
def test_actual_budget_and_unknown_reservation_cannot_be_ignored(field):
    usage = dict(
        active=0,
        model_calls=1,
        tool_calls=1,
        tokens=100,
        cost_micro_usd=0,
        started=dict(screen=1, reduce=0, confirm=0, regression=0),
    )
    budget = dict(
        max_model_calls=12,
        max_tool_calls=12,
        max_tokens=1000,
        max_cost_micro_usd=0,
        max_trials=30,
        quotas=dict(screen=14, reduce=6, confirm=6, regression=4),
    )
    outcome = dict(source=dict(usage=usage), budget=budget)
    budget_checks(outcome)
    if field == "phase":
        usage["started"]["screen"] = 15
    elif field == "active":
        usage["active"] = 1
    else:
        usage[field] = budget["max_" + field] + 1
    with pytest.raises(AssertionError):
        budget_checks(outcome)
