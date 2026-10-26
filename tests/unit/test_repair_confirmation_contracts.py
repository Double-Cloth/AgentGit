"""实际确认收据不能借用 Recorded、旧 Run 或零新模型调用。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_repair_study_contracts import receipt as unstarted
from test_repair_validation import digest, usage

from agentgit.core.experiments import FrozenAssessment
from agentgit.core.repair_confirmation import (
    RepairValidationReceipt,
    ValidationExecutionProof,
    ValidationModelCall,
)
from agentgit.core.research import Evaluation, evolve


def cancelled():
    return dict(
        **unstarted(),
        sample_id=str(uuid4()),
        phase="confirm",
        projection_ref=digest(10),
        provenance_ref=None,
        fresh_model_calls=0,
    )


def actual():
    body = cancelled()
    run = str(uuid4())
    body.update(
        not_started=False,
        job_id=str(uuid4()),
        initial_run_id=run,
        current_run_id=run,
        replay_report_ref=digest(11),
        provenance_ref=digest(12),
        category="oracle",
        verdict="passed",
        fresh_model_calls=1,
        usage=evolve(usage(), model_calls=1),
        assessment=FrozenAssessment(
            validator_ref=body["validator_ref"],
            run_id=run,
            commit_id=digest(13),
            state_ref=digest(14),
            verdict="passed",
            evaluation=Evaluation(passed=True),
        ),
    )
    return body


def test_actual_confirmation_requires_own_job_provenance_and_measured_new_model_call():
    assert RepairValidationReceipt.model_validate(cancelled()).not_started
    assert RepairValidationReceipt.model_validate(actual()).verdict == "passed"


@pytest.mark.parametrize(
    "change",
    [
        {"fresh_model_calls": 0},
        {"provenance_ref": None},
        {"job_id": None},
        {"assessment": None},
        {"sample_ref": None},
        {"not_started": True},
        {"safety_violations": ("forbidden",)},
    ],
)
def test_oracle_cannot_hide_missing_execution_or_safety(change):
    with pytest.raises(ValidationError):
        RepairValidationReceipt.model_validate({**actual(), **change})


def test_recovery_ledger_is_limited_to_current_job_and_acyclic():
    run, first, second = str(uuid4()), str(uuid4()), str(uuid4())
    one = ValidationModelCall(
        call_id=first, run_id=run, origin_call_id=None, intent_ref=digest(1), result_ref=digest(2)
    )
    two = evolve(one, call_id=second, origin_call_id=first)
    body = dict(
        trial_id=str(uuid4()),
        sample_id=str(uuid4()),
        protocol_ref=digest(3),
        projection_ref=digest(4),
        job_id=str(uuid4()),
        initial_run_id=run,
        current_run_id=run,
        run_ids=(run,),
        model_calls=(one, two),
    )
    assert len(ValidationExecutionProof(**body).model_calls) == 2
    for calls in (
        (evolve(one, origin_call_id=second), two),
        (evolve(one, origin_call_id=str(uuid4())),),
        (one, evolve(two, run_id=str(uuid4()))),
        (one, one),
    ):
        with pytest.raises(ValidationError):
            ValidationExecutionProof.model_validate({**body, "model_calls": calls})


def test_recorded_or_unstarted_physical_cost_cannot_be_confirmation():
    body = cancelled()
    with pytest.raises(ValidationError):
        RepairValidationReceipt.model_validate({**body, "usage": usage()})
    body = actual()
    with pytest.raises(ValidationError):
        RepairValidationReceipt.model_validate(
            {**body, "usage": evolve(body["usage"], cost_basis="recorded_no_external_calls")}
        )
