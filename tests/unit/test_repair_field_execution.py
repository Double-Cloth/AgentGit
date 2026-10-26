"""实际字段执行协议拒绝跨 Job 来源、循环和虚假新调用。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_repair_fields import setup_state

from agentgit.core.repair import BudgetDecision, RepairTrialUsage
from agentgit.core.repair_confirmation import ValidationModelCall
from agentgit.core.repair_fields import RepairFieldProof, RepairFieldReceipt, RepairFieldState
from agentgit.core.repair_studies import NewRepairStudy, RepairReductionReceipt
from agentgit.core.research import evolve
from agentgit.repair.field_reduction import summarize_fields


def proof():
    run, restored = str(uuid4()), str(uuid4())
    first = ValidationModelCall(
        call_id=str(uuid4()),
        run_id=run,
        origin_call_id=None,
        intent_ref="a" * 64,
        result_ref="b" * 64,
    )
    copied = evolve(first, call_id=str(uuid4()), run_id=restored, origin_call_id=first.call_id)
    return RepairFieldProof(
        trial_id=str(uuid4()),
        protocol_ref="c" * 64,
        candidate_ref="d" * 64,
        job_id=str(uuid4()),
        initial_run_id=run,
        current_run_id=restored,
        run_ids=(restored, run),
        model_calls=(first, copied),
    )


@pytest.mark.parametrize("change", ["run", "origin", "cycle", "duplicate", "family"])
def test_proof_rejects_external_response_or_invalid_own_recovery_chain(change):
    valid = proof()
    first, copied = valid.model_calls
    with pytest.raises(ValidationError):
        if change == "run":
            evolve(valid, model_calls=(evolve(first, run_id=str(uuid4())), copied))
        elif change == "origin":
            evolve(valid, model_calls=(first, evolve(copied, origin_call_id=str(uuid4()))))
        elif change == "cycle":
            evolve(valid, model_calls=(evolve(first, origin_call_id=copied.call_id), copied))
        elif change == "duplicate":
            evolve(valid, model_calls=(first, first))
        else:
            evolve(valid, run_ids=tuple(reversed(valid.run_ids)))


def receipt():
    return RepairFieldReceipt(
        execution=RepairReductionReceipt(
            study_id=str(uuid4()),
            trial_id=str(uuid4()),
            job_id=str(uuid4()),
            initial_run_id=str(uuid4()),
            current_run_id=str(uuid4()),
            protocol_ref="a" * 64,
            candidate_ref="b" * 64,
            sample_ref="c" * 64,
            validator_ref="d" * 64,
            verdict="incomplete",
            category="execution",
            replay_report_ref="e" * 64,
            usage=RepairTrialUsage(
                model_calls=1,
                tool_calls=0,
                tokens=100,
                cost_micro_usd=0,
                unit="mock_utf8_bytes",
                cost_basis="local_mock_free",
            ),
        ),
        provenance_ref="f" * 64,
        fresh_model_calls=1,
    )


def test_actual_receipt_requires_proof_and_exact_fresh_physical_count():
    valid = receipt()
    with pytest.raises(ValidationError):
        evolve(valid, provenance_ref=None)
    with pytest.raises(ValidationError):
        evolve(valid, fresh_model_calls=2)
    zero = evolve(valid.execution, usage=evolve(valid.execution.usage, model_calls=0))
    assert evolve(valid, execution=zero, fresh_model_calls=0).fresh_model_calls == 0


@pytest.mark.parametrize(
    "allowed,reason,maximum",
    [(True, "available", 0), (False, "concurrency", 0), (False, "tokens", 1)],
)
def test_parent_budget_stop_requires_real_denial_without_extra_field_allowance(
    allowed, reason, maximum
):
    state, _ = setup_state()
    with pytest.raises(ValidationError):
        evolve(
            state.plan,
            max_trials=maximum,
            parent_budget_stop=BudgetDecision(allowed=allowed, reason=reason, protected_trials=10),
        )


def test_parent_budget_stop_does_not_inherit_old_candidate_minimality():
    state, _ = setup_state()
    plan = evolve(
        state.plan,
        max_trials=0,
        initial_atomic_minimality="not_proven",
        parent_budget_stop=BudgetDecision(allowed=False, reason="tokens", protected_trials=10),
    )
    summary = summarize_fields(RepairFieldState(plan=plan))
    assert summary.status == "budget_exhausted" and summary.reason == "search_budget"
    assert summary.atomic_minimality == summary.field_minimality == "not_proven"


def test_new_studies_enable_fields_but_explicit_legacy_encoding_omits_policy():
    search = str(uuid4())
    assert NewRepairStudy(search_id=search).field_policy == "predefined-fields/v1"
    assert "field_policy" not in NewRepairStudy(search_id=search, field_policy=None).model_dump(
        mode="json"
    )
