"""字段阶段的共享额度、提交恢复、未知预留和取消，均使用实际 Job。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_repair_studies import drive_study, screening
from test_repair_worker import ConditionalMock

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import evolve
from agentgit.services.jobs import JobService
from agentgit.services.repair_field_trials import RepairFieldTrialService
from agentgit.services.repair_fields import TABLES, RepairFieldService
from agentgit.services.repair_reductions import RepairReductionService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def begin_field(history, tmp_path):
    repo, search, research, source = screening(history, tmp_path, mode="prompt")
    ledger = RepairStudyService(history)
    study = UUID(
        ledger.submit(repo, NewRepairStudy(search_id=str(search)), actor="fixture", key="study")[
            "study_id"
        ]
    )
    lease = ledger.claim("field-control", repository=repo)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    atomic = RepairReductionService(research)
    atomic.dispatch(lease, identity)
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    assert atomic.settle(lease, identity)["settled"]
    fields = RepairFieldService(history)
    fields.initialize(lease)
    identity = UUID(fields.reserve(lease)["trial_id"])
    return repo, study, research, ledger, fields, lease, identity, source


def count(history, name):
    with history.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(TABLES[name])).scalar_one()


@pytest.mark.parametrize("point", ["after_job_created", "after_dispatch_commit"])
def test_field_dispatch_commit_or_lost_ack_reuses_exact_job(history, tmp_path, point):
    repo, study, research, ledger, fields, lease, identity, _ = begin_field(history, tmp_path)
    before = count(history, "jobs")

    def stop(stage):
        if stage == point:
            raise RuntimeError("字段派发中断")

    trials = RepairFieldTrialService(research, fault=stop)
    with pytest.raises(RuntimeError):
        trials.dispatch(lease, identity)
    assert count(history, "jobs") == before + (point == "after_dispatch_commit")
    trials.fault = lambda _: None
    saved = trials.dispatch(lease, identity)
    assert trials.dispatch(lease, identity)["job_id"] == saved["job_id"]
    assert count(history, "repair_field_jobs") == 1
    assert ledger.view(repo, study)["usage"]["active"] == 1
    assert fields.reserve(lease)["trial_id"] == str(identity)


@pytest.mark.parametrize("point", ["before_result_commit", "after_result_commit"])
def test_field_result_and_state_commit_together_without_duplicate_charge(history, tmp_path, point):
    repo, study, research, ledger, _, lease, identity, _ = begin_field(history, tmp_path)
    trials = RepairFieldTrialService(research)
    trials.dispatch(lease, identity)
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    before = ledger.view(repo, study)["usage"]["tokens"]

    def stop(stage):
        if stage == point:
            raise RuntimeError("字段结算中断")

    trials.fault = stop
    with pytest.raises(RuntimeError):
        trials.settle(lease, identity)
    view = ledger.view(repo, study)
    assert len(view["field_reduction"]["state"]["steps"]) == int(point == "after_result_commit")
    assert (view["usage"]["active"] == 0) == (point == "after_result_commit")
    trials.fault = lambda _: None
    saved = trials.settle(lease, identity)
    assert trials.settle(lease, identity)["reused"]
    assert ledger.view(repo, study)["usage"]["tokens"] == (
        before + saved["result"]["execution"]["usage"]["tokens"]
    )
    # 结算后到达的旧未知观察不能重新挂起已完成的试验。
    assert trials.waiting(lease, identity, "usage_unknown")["settled"]
    assert ledger.view(repo, study)["status"] == "reducing"
    with pytest.raises(DBAPIError), history.engine.begin() as connection:
        connection.execute(
            TABLES["repair_field_results"]
            .update()
            .where(TABLES["repair_field_results"].c.id == identity)
            .values(tokens=0)
        )


@pytest.mark.parametrize("dispatched", [False, True])
def test_field_cancel_releases_only_proven_actual_or_empty_usage(history, tmp_path, dispatched):
    repo, study, research, ledger, _, lease, identity, _ = begin_field(history, tmp_path)
    trials = RepairFieldTrialService(research)
    before = ledger.view(repo, study)["usage"]["tokens"]
    if dispatched:
        trials.dispatch(lease, identity)
    ledger.cancel(repo, study, actor="operator")
    trials.cancel_children(lease)
    view = ledger.view(repo, study)
    if dispatched:
        assert view["status"] == "cancelling" and view["usage"]["active"] == 1
        Worker(JobService(research), model_factory=ConditionalMock).run_once()
        trials.settle(lease, identity)
        trials.cancel_children(lease)
        view = ledger.view(repo, study)
    assert view["status"] == "cancelled" and view["usage"]["active"] == 0
    receipt = view["field_reduction"]["trials"][0]["result"]
    assert receipt["execution"]["not_started"] == (not dispatched)
    assert view["usage"]["tokens"] == before
    assert view["final_lock"] is None
    assert len(view["field_reduction"]["trials"]) == 1


@pytest.mark.parametrize("maximum", [0, 2, 6])
def test_field_and_atomic_rechecks_share_original_reduce_limit(history, tmp_path, maximum):
    repo, search, research, _ = screening(history, tmp_path, mode="joint")
    ledger = RepairStudyService(history)
    ledger.submit(
        repo,
        NewRepairStudy(search_id=str(search), max_reduce_trials=maximum),
        actor="fixture",
        key="study",
    )
    view = drive_study(research, repo)
    assert view["status"] == "awaiting_samples", view
    fields = view["field_reduction"]
    assert view["usage"]["started"]["reduce"] == maximum
    assert len(view["trials"]) + len(fields["trials"]) == maximum
    assert fields["summary"]["status"] == "budget_exhausted"
    assert fields["summary"]["field_minimality"] == "not_proven"
    assert fields["summary"]["global_optimum"] is False
    if maximum == 6:
        assert fields["summary"]["accepted_fields"] == 1
        assert fields["summary"]["atomic_minimality"] == "one_minimal_under_fixture"
    if maximum == 0:
        assert fields["summary"]["atomic_minimality"] == "not_proven"


def test_parent_budget_denial_freezes_zero_field_allowance_and_preserves_ten_reserves(
    history, tmp_path
):
    repo, search, research, _ = screening(
        history, tmp_path, mode="joint", budget_transform=lambda b: evolve(b, max_tokens=2_370_000)
    )
    RepairStudyService(history).submit(
        repo, NewRepairStudy(search_id=str(search)), actor="fixture", key="study"
    )
    view = drive_study(research, repo)
    fields = view["field_reduction"]
    assert view["status"] == "awaiting_samples"
    assert fields["state"]["plan"]["parent_budget_stop"]["allowed"] is False
    assert fields["state"]["plan"]["max_trials"] == 0
    assert not fields["trials"] and fields["summary"]["field_minimality"] == "not_proven"
    assert fields["summary"]["atomic_minimality"] == "not_proven"
    assert view["usage"]["tokens"] + 10 * 200_000 <= 2_370_000


def test_field_policy_cannot_be_added_to_legacy_study(history, tmp_path):
    repo, search, _, _ = screening(history, tmp_path, mode="prompt")
    ledger = RepairStudyService(history)
    ledger.submit(
        repo, NewRepairStudy(search_id=str(search), field_policy=None), actor="fixture", key="old"
    )
    lease = ledger.claim("legacy", repository=repo)
    with pytest.raises(AgentGitError) as error:
        RepairFieldService(history).initialize(lease)
    assert error.value.code == ErrorCode.FORBIDDEN
    assert count(history, "repair_field_states") == 0
