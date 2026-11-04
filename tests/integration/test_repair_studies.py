"""S10 真实独立删除试验、共享预算、控制租约与最终候选锁。"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_repair_worker import ConditionalMock, create_case, drive

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import evolve
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_reductions import RepairReductionService
from agentgit.services.repair_studies import TABLES, RepairStudyService

pytestmark = pytest.mark.integration


def screening(history, tmp_path, *, mode="joint", budget_transform=lambda b: b):
    repo, search, research, _ = create_case(
        history, tmp_path, mode=mode, budget_transform=budget_transform
    )
    result = drive(research, repo, search)
    assert result["status"] == "needs_confirmation"
    return repo, search, research, result


def begin(history, tmp_path, **kwargs):
    repo, search, research, source = screening(history, tmp_path, **kwargs)
    ledger = RepairStudyService(history)
    created = ledger.submit(
        repo,
        NewRepairStudy(search_id=str(search), field_policy=None),
        actor="study-test",
        key="start-study",
    )
    return repo, UUID(created["study_id"]), search, research, ledger, source


def drive_study(research, repository):
    from agentgit.worker import Worker

    runner = RepairStudyWorker(research)
    worker = Worker(JobService(research), model_factory=ConditionalMock)
    for _ in range(30):
        result = runner.run_once(repository=repository)
        assert result is not None
        if result["status"] in (
            "awaiting_samples",
            "needs_review",
            "budget_exhausted",
            "needs_attention",
            "cancelled",
        ):
            assert worker.run_once() is None
            return result
        worker.run_once()
    pytest.fail("持久缩减没有在冻结额度内终止")


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in (
                "jobs",
                "runs",
                "repair_studies",
                "repair_reduction_trials",
                "repair_reduction_jobs",
                "repair_reduction_results",
                "repair_study_locks",
            )
        }


def test_real_two_atom_deletions_have_new_runs_and_lock_exact_candidate_without_opening_confirm(
    history, tmp_path
):
    repo, study, search, research, ledger, source = begin(history, tmp_path)
    branch = history.branch(repo)
    result = drive_study(research, repo)
    assert result["status"] == "awaiting_samples"
    assert result["reduction"]["minimality"] == "one_minimal_under_fixture"
    assert (
        len(result["state"]["initial"]["atoms"])
        == len(result["final_lock"]["lock"]["candidate"]["atoms"])
        == 2
    )
    assert len(result["trials"]) == 2 and all(
        t["result"]["verdict"] == "failed" for t in result["trials"]
    )
    new_runs = {t["result"]["current_run_id"] for t in result["trials"]}
    old_runs = {t["result"]["current_run_id"] for t in source["trials"]}
    assert len(new_runs) == 2 and new_runs.isdisjoint(old_runs)
    assert result["usage"]["started"]["screen"] == source["usage"]["started"]["screen"]
    assert result["usage"]["started"]["reduce"] == 2 and result["usage"]["started"]["confirm"] == 0
    assert result["usage"]["tokens"] == source["usage"]["tokens"] + sum(
        t["result"]["usage"]["tokens"] for t in result["trials"]
    )
    assert result["usage"]["active"] == 0 and result["usage"]["cost_micro_usd"] == 0
    assert RepairJobService(history).view(repo, search)["decision"] == source["decision"]
    assert history.branch(repo) == branch and ledger.claim("other", repository=repo) is None
    destination = os.environ.get("AGENTGIT_S10_REDUCTION_EVIDENCE")
    if destination:
        from diagnosis_evidence import pack_repository_evidence

        from agentgit.services.diagnosis import BehaviorService

        source_run = UUID(source["manifest"]["space"]["binding"]["failed_run_id"])
        graph = BehaviorService(history).trace(repo, source_run).model_dump(mode="json")
        evidence = dict(
            repository_id=str(repo),
            failed_run_id=str(source_run),
            reference_run_id=str(source_run),
            reference_role="source_alias_for_archive_only",
            graph=graph,
            reference_graph=graph,
            case_id="S10-two-atom-deletions/v1",
        )
        raw = pack_repository_evidence(history, evidence)
        from agentgit.services.management import public

        with history.engine.connect() as connection:
            indexes = {
                name: [
                    public(dict(row))
                    for row in connection.execute(sa.select(TABLES[name])).mappings()
                ]
                for name in (
                    "repair_studies",
                    "repair_study_jobs",
                    "repair_reduction_trials",
                    "repair_reduction_jobs",
                    "repair_reduction_results",
                    "repair_study_locks",
                    "repair_study_events",
                    "repair_reduction_waits",
                    "jobs",
                    "replay_requests",
                )
            }
        with Path(destination).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(
                    schema_version=1,
                    source_search=source,
                    study=result,
                    indexes=indexes,
                    evidence=evidence,
                    raw_repository=raw,
                ),
                handle,
                ensure_ascii=False,
                indent=2,
            )


def test_shared_source_token_consumption_stops_later_delete_but_keeps_confirmation_reserves(
    history, tmp_path
):
    repo, _, _, research, _, source = begin(
        history, tmp_path, budget_transform=lambda b: evolve(b, max_tokens=2_370_000)
    )
    result = drive_study(research, repo)
    assert result["status"] == "awaiting_samples" and len(result["trials"]) == 1
    assert result["reduction"]["status"] == "budget_exhausted"
    assert (
        result["reduction"]["reason"] == "search_budget"
        and result["reduction"]["minimality"] == "not_proven"
    )
    assert len(result["reduction"]["untested_final_deletions"]) == 1
    assert result["usage"]["tokens"] > source["usage"]["tokens"]
    assert result["usage"]["tokens"] + 10 * 200_000 <= 2_370_000


def test_max_reduce_override_cannot_expand_original_quota_and_source_remains_immutable(
    history, tmp_path
):
    repo, search, research, source = screening(history, tmp_path)
    ledger = RepairStudyService(history)
    before = counts(history)
    with pytest.raises(AgentGitError) as error:
        ledger.submit(
            repo,
            NewRepairStudy(search_id=str(search), max_reduce_trials=7),
            actor="operator",
            key="too-many",
        )
    assert error.value.code == ErrorCode.FORBIDDEN and counts(history) == before
    created = ledger.submit(
        repo,
        NewRepairStudy(search_id=str(search), max_reduce_trials=1),
        actor="operator",
        key="one",
    )
    result = drive_study(research, repo)
    assert (
        result["study_id"] == created["study_id"]
        and result["reduction"]["minimality"] == "not_proven"
    )
    assert len(result["trials"]) == 1 and result["final_lock"]
    assert RepairJobService(history).view(repo, search)["decision"] == source["decision"]


def test_six_same_requests_single_pool_publish_once_and_readonly_scope_is_enforced(
    history, tmp_path, monkeypatch
):
    repo, search, research, _ = screening(history, tmp_path, mode="prompt")
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    engine = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        ledger = RepairStudyService(HistoryService(engine, history.objects))
        body = NewRepairStudy(search_id=str(search))
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(
                pool.map(lambda _: ledger.submit(repo, body, actor="single", key="same"), range(6))
            )
        assert all(r == rows[0] for r in rows) and counts(history)["repair_studies"] == 1
        identity = UUID(rows[0]["study_id"])
        with pytest.raises(AgentGitError):
            ledger.submit(repo, evolve(body, max_reduce_trials=1), actor="single", key="same")
        with pytest.raises(AgentGitError):
            ledger.submit(repo, body, actor="single", key="second")
        with pytest.raises(AgentGitError) as error:
            ledger.view(uuid4(), identity)
        assert error.value.code == ErrorCode.NOT_FOUND

        def readonly(*args, **kwargs):
            pytest.fail("只读缩减查询不能写 CAS")

        monkeypatch.setattr(history.objects, "put", readonly)
        assert ledger.view(repo, identity)["status"] == "queued"
    finally:
        engine.dispose()


def test_unresolved_actual_usage_stays_reserved_and_old_observation_cannot_rehang_settled_state(
    history, tmp_path, monkeypatch
):
    from agentgit.worker import Worker

    repo, study, _, research, ledger, _ = begin(history, tmp_path, mode="prompt")
    runner = RepairStudyWorker(research)
    first = runner.run_once(repository=repo)
    identity = UUID(first["trials"][0]["trial_id"])
    assert (
        Worker(JobService(research), model_factory=ConditionalMock).run_once()["status"] == "failed"
    )
    with monkeypatch.context() as unknown:
        unknown.setattr(runner.trials, "_usage", lambda report: None)
        waiting = runner.run_once(repository=repo)
        assert waiting["status"] == "waiting_reconciliation" and waiting["usage"]["active"] == 1
        assert len(runner.run_once(repository=repo)["trials"]) == 1
    finished = runner.run_once(repository=repo)
    assert finished["status"] == "awaiting_samples" and finished["usage"]["active"] == 0
    assert finished["trials"][0]["trial_id"] == str(identity)
    assert ledger.view(repo, study)["final_lock"]


@pytest.mark.parametrize("point", ["after_job_created", "after_dispatch_commit"])
def test_dispatch_rollback_or_lost_ack_reuses_intent_and_independent_job(history, tmp_path, point):
    repo, study, _, research, ledger, _ = begin(history, tmp_path, mode="prompt")
    lease = ledger.claim("manual", repository=repo)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    service = RepairReductionService(research)
    before = counts(history)

    def stop(stage):
        if stage == point:
            raise RuntimeError("模拟派发边界中断")

    service.fault = stop
    with pytest.raises(RuntimeError):
        service.dispatch(lease, identity)
    assert counts(history)["jobs"] == before["jobs"] + (point == "after_dispatch_commit")
    service.fault = lambda _: None
    first = service.dispatch(lease, identity)
    assert service.dispatch(lease, identity)["job_id"] == first["job_id"]
    assert counts(history)["repair_reduction_jobs"] == 1
    assert ledger.view(repo, study)["usage"]["active"] == 1


@pytest.mark.parametrize("point", ["before_result_commit", "after_result_commit"])
def test_result_and_protocol_advance_commit_together_and_retry_does_not_charge_twice(
    history, tmp_path, point
):
    from agentgit.worker import Worker

    repo, study, _, research, ledger, source = begin(history, tmp_path, mode="prompt")
    lease = ledger.claim("manual", repository=repo)
    service = RepairReductionService(research)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    service.dispatch(lease, identity)
    Worker(JobService(research), model_factory=ConditionalMock).run_once()

    def stop(stage):
        if stage == point:
            raise RuntimeError("模拟结算边界中断")

    service.fault = stop
    with pytest.raises(RuntimeError):
        service.settle(lease, identity)
    before = ledger.view(repo, study)
    assert len(before["state"]["steps"]) == int(point == "after_result_commit")
    assert (before["usage"]["active"] == 0) == (point == "after_result_commit")
    service.fault = lambda _: None
    settled = service.settle(lease, identity)
    assert service.settle(lease, identity)["reused"]
    finished = ledger.view(repo, study)
    assert (
        finished["usage"]["tokens"]
        == source["usage"]["tokens"] + settled["result"]["usage"]["tokens"]
    )


def test_expired_controller_is_fenced_and_unstarted_cancel_releases_only_proven_empty_intent(
    history, tmp_path
):
    repo, study, _, research, ledger, source = begin(history, tmp_path, mode="prompt")
    short = RepairStudyService(history, lease_ms=100)
    old = short.claim("old", repository=repo)
    time.sleep(0.15)
    lease = ledger.claim("new", repository=repo)
    with pytest.raises(AgentGitError) as error:
        short.reserve(old)
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    ledger.reserve(lease)
    ledger.cancel(repo, study, actor="operator")
    RepairReductionService(research).cancel_children(lease)
    view = ledger.view(repo, study)
    assert view["status"] == "cancelled" and view["usage"]["active"] == 0
    assert view["usage"]["started"]["reduce"] == 1 and not view["state"]["steps"]
    assert view["trials"][0]["result"]["not_started"]
    assert view["usage"]["tokens"] == source["usage"]["tokens"]


def test_dispatched_cancel_waits_for_actual_worker_and_never_expands(history, tmp_path):
    from agentgit.worker import Worker

    repo, study, _, research, ledger, _ = begin(history, tmp_path, mode="prompt")
    runner = RepairStudyWorker(research)
    first = runner.run_once(repository=repo)
    ledger.cancel(repo, study, actor="operator")
    waiting = runner.run_once(repository=repo)
    assert waiting["status"] == "cancelling" and waiting["usage"]["active"] == 1
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    finished = runner.run_once(repository=repo)
    assert finished["status"] == "cancelled" and finished["usage"]["active"] == 0
    assert len(finished["trials"]) == len(first["trials"]) == 1


def test_database_rejects_history_rewrite_and_success_for_unexecuted_reduction(history, tmp_path):
    repo, study, _, _, ledger, _ = begin(history, tmp_path, mode="prompt")
    with pytest.raises(DBAPIError), history.engine.begin() as connection:
        connection.execute(
            TABLES["repair_studies"]
            .update()
            .where(TABLES["repair_studies"].c.id == study)
            .values(actor="forged")
        )
    lease = ledger.claim("manual", repository=repo)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    obj = history.objects.put_json(
        ObjectType.BLOB,
        {"schema_version": 1, "fake": True},
    )
    with pytest.raises(DBAPIError), history.engine.begin() as connection:
        history._ref(connection, repo, obj, "repair_reduction_trial", str(identity), "result")
        history._ref(connection, repo, obj, "repair_reduction_trial", str(identity), "evidence")
        connection.execute(
            TABLES["repair_reduction_results"]
            .insert()
            .values(
                id=identity,
                repository_id=repo,
                result_ref=obj.id,
                evidence_ref=obj.id,
                not_started=False,
                verdict="passed",
                model_calls=0,
                tool_calls=0,
                tokens=0,
                cost_micro_usd=0,
            )
        )


def test_preparation_cancel_prevents_new_job(history, tmp_path, monkeypatch):
    repo, study, _, research, ledger, _ = begin(history, tmp_path, mode="prompt")
    lease = ledger.claim("manual", repository=repo)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    service = RepairReductionService(research)
    prepare = service.replays.prepare

    def cancel_after_prepare(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        ledger.cancel(repo, study, actor="operator")
        return prepared

    monkeypatch.setattr(service.replays, "prepare", cancel_after_prepare)
    before = counts(history)
    with pytest.raises(AgentGitError):
        service.dispatch(lease, identity)
    assert (
        counts(history)["jobs"] == before["jobs"] and counts(history)["repair_reduction_jobs"] == 0
    )
