"""确认池契约与完整最坏预算；与真实数据库套件使用不同模块名。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_repair_validation import digest

from agentgit.core.repair import PhaseQuotas, RepairBudget, RepairConsumption
from agentgit.core.repair_pools import (
    NewRepairValidationSuite,
    RegisteredValidationSample,
    RepairSamplingTicket,
)
from agentgit.core.repair_validation import ValidationSample, validation_ref
from agentgit.core.research import evolve
from agentgit.repair.pools import validation_preflight


def ticket():
    return RepairSamplingTicket(
        generation_id=str(uuid4()),
        source_run_id=str(uuid4()),
        from_commit=digest(1),
        task_ref=digest(2),
        validator_ref=digest(3),
        corpus_ref=digest(4),
        environment_ref=digest(5),
        source_state_ref=digest(6),
        source_config_ref=digest(7),
        model_conditions_ref=digest(8),
        code_revision="source-sha256:" + "1" * 64,
        dependency_lock_digest=digest(9),
        execution_mode="live",
    )


def test_ticket_identity_contains_generation_conditions_and_no_role_or_generated_response():
    value = ticket()
    assert value.response_status == "not_generated"
    assert "role" not in value.model_dump() and "response_ref" not in value.model_dump()
    sample = ValidationSample(
        role="confirm",
        task_ref=value.task_ref,
        response_set_ref=validation_ref(value),
        validator_ref=value.validator_ref,
    )
    assert RegisteredValidationSample(ticket=value, sample=sample).sample == sample
    for change in (
        {"response_status": "passed"},
        {"role": "confirm"},
        {"response_ref": digest(10)},
    ):
        with pytest.raises(ValidationError):
            RepairSamplingTicket.model_validate({**value.model_dump(), **change})
    for change in (
        {"task_ref": digest(10)},
        {"validator_ref": digest(10)},
        {"response_set_ref": digest(10)},
        {"role": "reduce"},
    ):
        with pytest.raises(ValidationError):
            RegisteredValidationSample(ticket=value, sample=evolve(sample, **change))


@pytest.mark.parametrize(
    "field,reason",
    [
        ("max_model_calls", "model_calls"),
        ("max_tool_calls", "tool_calls"),
        ("max_tokens", "tokens"),
        ("max_wall_ms", "wall_time"),
    ],
)
def test_preflight_reserves_all_ten_trials_in_every_resource(field, reason):
    budget = RepairBudget()
    per = getattr(budget.trial, field)
    changed = evolve(budget, **{field: per * 10 - 1})
    decision = validation_preflight(changed, RepairConsumption())
    assert not decision.allowed and decision.reason == reason and decision.protected_trials == 10


def test_preflight_counts_prior_consumption_and_phase_quota_and_full_wall_clock():
    budget = RepairBudget()
    assert validation_preflight(budget, RepairConsumption()).allowed
    used = RepairConsumption(tokens=20_001)
    assert validation_preflight(budget, used).reason == "tokens"
    small = evolve(budget, quotas=evolve(budget.quotas, confirm=5))
    assert validation_preflight(small, RepairConsumption()).reason == "phase_trials"
    used = RepairConsumption(started=PhaseQuotas(screen=14, reduce=6, confirm=1, regression=0))
    assert validation_preflight(budget, used).reason == "phase_trials"
    used = RepairConsumption(started=PhaseQuotas(screen=14, reduce=7, confirm=0, regression=0))
    assert validation_preflight(budget, used).reason == "total_trials"


def test_duplicate_regression_origin_or_uploaded_result_is_rejected():
    origin = dict(schema_version=1, source_run_id=str(uuid4()), from_commit=digest(2))
    with pytest.raises(ValidationError):
        NewRepairValidationSuite(search_id=str(uuid4()), regression=(origin,) * 4)
