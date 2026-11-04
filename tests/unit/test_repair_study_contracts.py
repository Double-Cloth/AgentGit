"""持久缩减收据必须绑定真实阶段身份；总预算提前停止不能制造最小性。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_repair_validation import digest, state, usage

from agentgit.core.errors import AgentGitError
from agentgit.core.repair import BudgetDecision
from agentgit.core.repair_studies import RepairReductionReceipt
from agentgit.core.research import evolve
from agentgit.repair.reduction import lock_reduced_candidate, summarize_reduction


def receipt():
    return dict(
        study_id=str(uuid4()),
        trial_id=str(uuid4()),
        job_id=None,
        initial_run_id=None,
        current_run_id=None,
        protocol_ref=digest(1),
        candidate_ref=digest(2),
        sample_ref=digest(3),
        validator_ref=digest(4),
        verdict="incomplete",
        category="cancelled",
        usage=evolve(usage(), model_calls=0, tokens=0),
        not_started=True,
        replay_report_ref=None,
    )


def test_only_proven_unstarted_cancel_can_have_no_job_or_replay_report():
    body = receipt()
    assert RepairReductionReceipt.model_validate(body).not_started
    with pytest.raises(ValidationError):
        RepairReductionReceipt.model_validate({**body, "not_started": False})


@pytest.mark.parametrize(
    "change",
    [
        {"verdict": "passed"},
        {"category": "oracle"},
        {"job_id": str(uuid4())},
        {"usage": usage()},
        {"safety_violations": ("forbidden",)},
    ],
)
def test_unstarted_cannot_hide_execution_evidence_or_cost(change):
    with pytest.raises(ValidationError):
        RepairReductionReceipt.model_validate({**receipt(), **change})


def test_global_budget_stop_can_lock_existing_passed_candidate_without_minimality():
    current = state()
    stop = BudgetDecision(allowed=False, reason="tokens", protected_trials=10)
    summary = summarize_reduction(current, budget_stop=stop)
    assert summary.status == "budget_exhausted" and summary.reason == "search_budget"
    assert summary.minimality == "not_proven" and len(summary.untested_final_deletions) == 3
    locked = lock_reduced_candidate(current, selection_event_ref=digest(999), budget_stop=stop)
    assert locked.candidate == current.initial


@pytest.mark.parametrize(
    "allowed,reason", [(True, "available"), (False, "available"), (False, "concurrency")]
)
def test_inflight_or_available_resource_cannot_fake_budget_stop(allowed, reason):
    with pytest.raises(AgentGitError):
        summarize_reduction(
            state(), budget_stop=BudgetDecision(allowed=allowed, reason=reason, protected_trials=10)
        )
