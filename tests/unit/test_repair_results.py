"""恢复正确、任务通过、预算停止与未知不能在结果契约中相互冒充。"""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.repair import RepairTrialResult


def result_body():
    run_id = str(uuid4())
    return {
        "trial_id": str(uuid4()),
        "job_id": str(uuid4()),
        "initial_run_id": run_id,
        "current_run_id": run_id,
        "operation": "screen",
        "verdict": "passed",
        "category": "oracle",
        "determinate": True,
        "replay_report_ref": "a" * 64,
        "usage": {
            "model_calls": 2,
            "tool_calls": 1,
            "tokens": 100,
            "cost_micro_usd": 0,
            "unit": "mock_utf8_bytes",
            "cost_basis": "local_mock_free",
        },
        "assessment": {
            "validator_ref": "b" * 64,
            "run_id": run_id,
            "commit_id": "c" * 64,
            "state_ref": "d" * 64,
            "verdict": "passed",
            "evaluation": {"passed": True},
        },
    }


def recovery_body():
    body = result_body()
    body.update(
        operation="recorded_preflight", verdict="chain_valid", category="recovery", assessment=None
    )
    body["usage"].update(
        model_calls=0, tool_calls=0, tokens=0, cost_basis="recorded_no_external_calls"
    )
    return body


def parse(body):
    return RepairTrialResult.model_validate_json(encode(body))


def test_passed_trial_is_a_test_only_frozen_assessment_and_not_a_production_verification():
    result = parse(result_body())
    assert result.verdict == "passed" and result.test_only
    assert result.assessment.run_id == result.current_run_id
    assert "verified" not in result.model_dump()


@pytest.mark.parametrize(
    "change",
    [
        {"assessment": None},
        {"safety_violations": ["forbidden"]},
        {"determinate": False},
        {"category": "budget"},
        {"category": "cancelled"},
        {"category": "recovery"},
        {"verified": True},
        {"test_only": False},
    ],
)
def test_missing_evidence_unsafe_or_unconfirmed_result_cannot_claim_pass(change):
    body = result_body()
    body.update(change)
    with pytest.raises(ValidationError):
        parse(body)


def test_assessment_cannot_be_borrowed_from_another_run_or_disagree_with_result():
    body = result_body()
    body["assessment"]["run_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        parse(body)
    body = result_body()
    body["verdict"] = "failed"
    with pytest.raises(ValidationError):
        parse(body)


def test_recorded_chain_validation_does_not_assert_task_success():
    result = parse(recovery_body())
    assert result.assessment is None and result.category == "recovery"
    assert result.usage.model_calls == result.usage.tokens == 0


@pytest.mark.parametrize("field", ["model_calls", "tool_calls", "tokens", "cost_micro_usd"])
def test_recorded_proof_cannot_hide_new_external_usage(field):
    body = recovery_body()
    body["usage"][field] = 1
    with pytest.raises(ValidationError):
        parse(body)


@pytest.mark.parametrize(
    "change",
    [
        {"determinate": False},
        {"category": "oracle"},
        {"operation": "screen"},
        {"verdict": "passed"},
    ],
)
def test_recovery_identity_and_scope_cannot_be_relabelled(change):
    body = recovery_body()
    body.update(change)
    with pytest.raises(ValidationError):
        parse(body)


@pytest.mark.parametrize("category", ["budget", "cancelled", "safety"])
def test_stopped_trial_preserves_usage_but_is_not_a_determinate_no_solution_observation(category):
    body = deepcopy(result_body())
    body.update(verdict="failed", category=category, determinate=False, assessment=None)
    result = parse(body)
    assert result.usage.tokens == 100 and not result.determinate
    body["determinate"] = True
    with pytest.raises(ValidationError):
        parse(body)
