"""真实持久试验中的供应商计量、检查点滞后和可信核对；供应商 HTTP 固定离线替身。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_model_runtime import commits, setup, snapshot

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.runtime.ports import VerifiedOutcome
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.reconciliation import ReconciliationService
from agentgit.services.replay_metering import physical_model_usage
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def prepared(history, tmp_path, monkeypatch, *, repeats=1):
    service, repo, config, queue, source, http = setup(history, tmp_path, monkeypatch)
    assert Worker(queue).run_once()["status"] == "completed"
    points = commits(history, repo)
    goal = snapshot(history, repo, queue, source).goal
    validator = ValidatorService(history).freeze(repo, goal, service.corpus)
    manager = ManagementService(service)
    result = manager.create_experiment(
        repo,
        NewExperiment(
            source_run_id=source.run_id,
            from_commit=points[3],
            validator_ref=validator,
            repeats=repeats,
            budget=config.limits,
            groups=(
                ExperimentGroup(key="A", label="基线"),
                ExperimentGroup(key="B", label="核验", prompt=config.prompt + "核验所有引用。"),
            ),
        ),
        "provider-statistics",
    )
    return manager, repo, result, http


@pytest.mark.parametrize("mode", ["ok", "unknown", "unpriced"])
def test_provider_statistics_keep_measured_estimated_and_unknown_cost_distinct(
    history, tmp_path, monkeypatch, mode
):
    repeats = 2 if mode == "ok" else 1
    manager, repo, submitted, http = prepared(history, tmp_path, monkeypatch, repeats=repeats)
    before = manager.experiment(repo, UUID(submitted["experiment_id"]))
    assert all(
        group["hard_assertions"]["not_evaluated"] == repeats
        for group in before["statistics"]["groups"]
    )
    http.mode = mode
    for _ in range(2 * repeats):
        http.generations = 0
        Worker(manager.jobs).run_once()
    report = manager.experiment(repo, UUID(submitted["experiment_id"]))
    for trial in report["trials"]:
        meter = trial["replay"]["actual"]["model_usage"]
        assert meter["mock"]["calls"] == 0
        provider = meter["provider"]
        assert provider["billed_cost_micro_usd"] is None
        if mode == "ok":
            assert trial["verdict"] == "passed"
            assert provider["calls"] == provider["measured_calls"] == 9
            assert provider["input_tokens"] == 900 and provider["output_tokens"] == 180
            assert provider["estimated_cost_micro_usd"] == 1242
            assert provider["reserved_cost_micro_usd"] == 0
        else:
            assert trial["verdict"] == ("incomplete" if mode == "unknown" else "failed")
            assert provider["calls"] == 1 and provider["estimated_cost_micro_usd"] is None
            assert provider["reserved_cost_micro_usd"] == 61440
            assert provider["input_tokens"] == (None if mode == "unknown" else 100)
    for group in report["statistics"]["groups"]:
        scope = group["consumption_to_date"]["metrics"]
        if mode == "ok":
            assert scope["provider.estimated_cost_micro_usd"]["sum"] == "2484"
            assert scope["provider.input_tokens"]["sample_variance"] == {
                "numerator": "0",
                "denominator": "1",
            }
            assert scope["provider.billed_cost_micro_usd"]["missing_samples"] == 2
        else:
            assert scope["provider.estimated_cost_micro_usd"]["sum"] is None
            assert scope["provider.estimated_cost_micro_usd"]["missing_samples"] == 1
            assert group["success_rate"] == {"numerator": "0", "denominator": "1"}
            assert group["assessed_trials"]["trial_count"] == (0 if mode == "unknown" else 1)
    if mode == "ok":
        calls, objects = TABLES["external_calls"], TABLES["objects"]
        with history.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    sa.select(calls, objects.c.byte_length.label("input_bytes"))
                    .select_from(calls.join(objects, objects.c.id == calls.c.request_digest))
                    .where(
                        calls.c.run_id == UUID(report["trials"][0]["run_id"]),
                        calls.c.provider.like("llm:%"),
                    )
                ).mappings()
            ]
        foreign = history.create_repository("禁止借用计量证据")
        with pytest.raises(AgentGitError) as denied:
            physical_model_usage(history, foreign, rows)
        assert denied.value.code == ErrorCode.RECORDING_MISSING


def defer_other_jobs(manager, job_id):
    with manager.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id != job_id)
            .values(available_at=sa.func.clock_timestamp() + sa.text("interval '1 hour'"))
        )


def test_saved_provider_result_is_metered_before_checkpoint_and_not_counted_again_after_recovery(
    history, tmp_path, monkeypatch
):
    manager, repo, submitted, _ = prepared(history, tmp_path, monkeypatch)

    def crash(point):
        if point == "after_result":
            raise RuntimeError("计量已持久化而检查点尚未保存")

    with pytest.raises(RuntimeError, match="计量已持久化"):
        Worker(manager.jobs, fault=crash).run_once()
    report = manager.experiment(repo, UUID(submitted["experiment_id"]))
    pending = next(trial for trial in report["trials"] if trial["status"] == "running")
    provider = pending["replay"]["actual"]["model_usage"]["provider"]
    assert provider["calls"] == 1 and provider["estimated_cost_micro_usd"] == 138
    assert pending["replay"]["new_usage"]["cost_micro_units"] == 0
    job_id = UUID(pending["job_id"])
    defer_other_jobs(manager, job_id)
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    after = manager.experiment(repo, UUID(submitted["experiment_id"]))
    recovered = next(trial for trial in after["trials"] if trial["job_id"] == str(job_id))
    provider = recovered["replay"]["actual"]["model_usage"]["provider"]
    assert recovered["run_id"] != pending["run_id"]
    assert provider["calls"] == 9 and provider["estimated_cost_micro_usd"] == 1242
    assert provider["reserved_cost_micro_usd"] == 0


def test_trusted_resolution_is_read_through_its_own_provenance_and_counted_once(
    history, tmp_path, monkeypatch
):
    manager, repo, submitted, http = prepared(history, tmp_path, monkeypatch)
    http.mode = "unknown"
    http.generations = 0
    assert Worker(manager.jobs).run_once()["status"] == "outcome_unknown"
    report = manager.experiment(repo, UUID(submitted["experiment_id"]))
    pending = next(trial for trial in report["trials"] if trial["status"] == "outcome_unknown")
    job_id = UUID(pending["job_id"])
    run_id = UUID(pending["run_id"])
    with history.engine.connect() as connection:
        call = dict(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == run_id,
                    TABLES["external_calls"].c.status == "outcome_unknown",
                )
            )
            .mappings()
            .one()
        )

    class Resolver:
        provider, signature = call["provider"], call["signature_digest"]

        def resolve(self, request):
            return VerifiedOutcome(http.receipts[request], b"offline-statistics-receipt")

    ReconciliationService(history).confirm(
        repo,
        run_id,
        call["id"],
        actor=manager.actor,
        expected_version=manager.jobs.run(repo, run_id)["state_version"],
        resolver=Resolver(),
    )
    defer_other_jobs(manager, job_id)
    http.mode = "ok"
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    result = manager.experiment(repo, UUID(submitted["experiment_id"]))
    resolved = next(trial for trial in result["trials"] if trial["job_id"] == str(job_id))
    provider = resolved["replay"]["actual"]["model_usage"]["provider"]
    assert provider["calls"] == 9 and provider["estimated_cost_micro_usd"] == 1242
    assert provider["unpriced_calls"] == provider["reserved_cost_micro_usd"] == 0
