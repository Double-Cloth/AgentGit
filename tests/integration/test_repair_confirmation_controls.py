"""确认实际调用的事务故障、未知预留、取消与跨阶段恢复。"""

import time
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_repair_confirmation import actual_worker, drive_confirmation, setup_confirmation

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import evolve
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.services.repair_confirm_trials import RepairConfirmationTrialService
from agentgit.services.repair_confirmation import TABLES, RepairConfirmationService
from agentgit.services.repair_studies import RepairStudyService

pytestmark = pytest.mark.integration


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in (
                "jobs",
                "runs",
                "repair_validation_trials",
                "repair_validation_jobs",
                "repair_validation_results",
            )
        }


def pending(history, tmp_path):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    control, trials = RepairConfirmationService(research), RepairConfirmationTrialService(research)
    lease = control.claim("controller", repository=repo)
    identity = UUID(control.reserve(lease)["trial_id"])
    return repo, study, research, control, trials, lease, identity


@pytest.mark.parametrize("point", ["after_job_created", "after_dispatch_commit"])
def test_dispatch_commit_failure_reuses_exact_intent_and_job(history, tmp_path, point):
    repo, study, research, control, trials, lease, identity = pending(history, tmp_path)
    before = counts(history)

    def fault(stage):
        if stage == point:
            raise RuntimeError("验证派发提交边界中断")

    trials.fault = fault
    with pytest.raises(RuntimeError):
        trials.dispatch(lease, identity)
    assert counts(history)["jobs"] == before["jobs"] + (point == "after_dispatch_commit")
    trials.fault = lambda _: None
    first = trials.dispatch(lease, identity)
    assert trials.dispatch(lease, identity)["job_id"] == first["job_id"]
    assert counts(history)["repair_validation_jobs"] == 1
    assert actual_worker(history, research, repo, "pass").run_once()["status"] == "completed"
    result = trials.settle(lease, identity)
    assert result["result"]["verdict"] == "passed"
    assert trials.settle(lease, identity)["result_ref"] == result["result_ref"]
    assert control.view(repo, study)["usage"]["started"]["confirm"] == 1


@pytest.mark.parametrize("point", ["before_result_commit", "after_result_commit"])
def test_receipt_usage_and_audit_commit_together_without_second_execution(history, tmp_path, point):
    repo, study, research, control, trials, lease, identity = pending(history, tmp_path)
    trials.dispatch(lease, identity)
    assert actual_worker(history, research, repo, "pass").run_once()["status"] == "completed"
    before = control.view(repo, study)

    def fault(stage):
        if stage == point:
            raise RuntimeError("验证结算提交边界中断")

    trials.fault = fault
    with pytest.raises(RuntimeError):
        trials.settle(lease, identity)
    assert counts(history)["repair_validation_results"] == (point == "after_result_commit")
    trials.fault = lambda _: None
    settled = trials.settle(lease, identity)
    after = control.view(repo, study)
    assert after["usage"]["active"] == 0
    assert (
        after["usage"]["tokens"] == before["usage"]["tokens"] + settled["result"]["usage"]["tokens"]
    )
    assert trials.settle(lease, identity)["result_ref"] == settled["result_ref"]
    assert actual_worker(history, research, repo, "pass").run_once() is None


def test_unknown_usage_keeps_full_reservation_and_stale_observation_cannot_rehang(
    history, tmp_path, monkeypatch
):
    repo, study, research, control, trials, lease, identity = pending(history, tmp_path)
    trials.dispatch(lease, identity)
    actual_worker(history, research, repo, "pass").run_once()
    with monkeypatch.context() as unknown:
        unknown.setattr(trials, "_usage", lambda _: None)
        assert not trials.settle(lease, identity)["settled"]
        view = control.view(repo, study)
        assert view["status"] == "waiting_reconciliation" and view["usage"]["active"] == 1
        with pytest.raises(AgentGitError) as error:
            control.reserve(lease)
        assert error.value.code == ErrorCode.CONTROL_REQUESTED
    result = trials.settle(lease, identity)
    assert result["settled"] and control.view(repo, study)["usage"]["active"] == 0
    assert (
        trials._waiting(lease, identity, "stale_observation")["result_ref"] == result["result_ref"]
    )
    assert control.view(repo, study)["status"] == "confirming"


def test_expired_lease_is_fenced_and_unstarted_cancel_releases_only_empty_intent(history, tmp_path):
    repo, study, research, control, trials, lease, identity = pending(history, tmp_path)
    before = counts(history)
    RepairStudyService(history, lease_ms=100).heartbeat(lease)
    time.sleep(0.15)
    replacement = control.claim("new-controller", repository=repo)
    assert replacement.token != lease.token
    with pytest.raises(AgentGitError) as error:
        trials.dispatch(lease, identity)
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    control.ledger.cancel(repo, study, actor="operator")
    trials.cancel_children(replacement)
    final = control.finish(replacement, reason="operator_cancelled")
    assert final["status"] == "cancelled" and final["usage"]["active"] == 0
    assert final["usage"]["started"]["confirm"] == 1
    assert final["verification"]["confirm_attempted"] == 0
    assert final["trials"][0]["result"]["not_started"]
    assert counts(history)["jobs"] == before["jobs"]


def test_dispatched_cancel_waits_for_worker_and_retains_actual_receipt(history, tmp_path):
    repo, study, research, control, trials, lease, identity = pending(history, tmp_path)
    trials.dispatch(lease, identity)
    control.ledger.cancel(repo, study, actor="operator")
    trials.cancel_children(lease)
    assert control.view(repo, study)["usage"]["active"] == 1
    assert actual_worker(history, research, repo, "pass").run_once()["status"] == "cancelled"
    result = trials.settle(lease, identity)["result"]
    assert result["category"] == "cancelled" and not result["not_started"]
    final = control.finish(lease, reason="operator_cancelled")
    assert final["status"] == "cancelled" and final["verification"]["confirm_attempted"] == 1
    assert final["verification"]["status"] == "needs_review"


def test_last_confirmation_reserved_before_restart_remains_confirming_until_settled(
    history, tmp_path
):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    runner, worker = (
        RepairConfirmationWorker(research),
        actual_worker(history, research, repo, "pass"),
    )
    for _ in range(5):
        assert runner.run_once(repository=repo)["status"] == "confirming"
        assert worker.run_once()["status"] == "completed"
    control, trials = runner.control, runner.trials
    lease = control.claim("manual-reservation", repository=repo)
    view = control.view(repo, study)
    last = UUID(view["trials"][-1]["trial_id"])
    trials.settle(lease, last)
    reserved = control.reserve(lease)
    sixth = UUID(reserved["trial_id"])
    control.ledger.release(lease)
    resumed = runner.run_once(repository=repo)
    assert resumed["status"] == "confirming" and resumed["trials"][-1]["trial_id"] == str(sixth)
    assert resumed["trials"][-1]["dispatch"]
    assert worker.run_once()["status"] == "completed"
    assert drive_confirmation(history, research, repo)["status"] == "verified_under_fixture"


def test_regression_trial_budget_failure_is_incomplete_and_never_verified(history, tmp_path):
    repo, _, research, _, _ = setup_confirmation(
        history,
        tmp_path,
        budget_transform=lambda budget: evolve(
            budget, trial=evolve(budget.trial, max_model_calls=2)
        ),
    )
    result = drive_confirmation(history, research, repo)
    assert result["status"] == "needs_review", result
    assert result["verification"]["confirm_passed"] == 6
    assert result["verification"]["regression_attempted"] == 4
    assert result["verification"]["regression_passed"] == 0
    assert "incomplete_regression_evidence" in result["verification"]["reasons"]
    assert all(trial["result"]["category"] == "budget" for trial in result["trials"][6:])
