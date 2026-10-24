"""真实独立试验的原子派发、恢复证明和物理结算；未运行完整搜索编排。"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from diagnosis_evidence import pack_repository_evidence
from test_debug_candidate_experiments import candidate_case as candidate_case
from test_recorded_replayer import replay_source as replay_source
from test_repair_ledger import repair_case as repair_case

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import RepairBinding, RepairBudget
from agentgit.core.research import BudgetLimits, evolve
from agentgit.core.versions import RecordedReplay
from agentgit.repair.space import freeze_space
from agentgit.repair.templates import TemplateAnchor, observation_templates
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService, public
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.repair_trials import RepairTrialService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def setup_trials(history, repair_case, *, concurrency=1, wall_ms=60_000):
    repo, space, identities = repair_case
    # 显式测试预算覆盖旧 Mock 的 UTF-8 请求/响应字节，不宣称默认 4,000 单位足够。
    limits = evolve(RepairBudget().trial, max_tokens=200_000, max_wall_ms=wall_ms)
    budget = evolve(RepairBudget(), trial=limits, max_tokens=6_000_000, max_concurrency=concurrency)
    created = RepairSearchService(history).submit(
        space, budget, identities, actor="repair-operator", key="real-trials"
    )
    search = UUID(created["search_id"])
    research = ResearchService(history, history.objects.root.parent)
    ledger = RepairJobService(history, lease_ms=120_000)
    lease = ledger.claim("trial-controller")
    service = RepairTrialService(research)
    return repo, search, ledger, lease, service, Worker(JobService(research))


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in (
                "jobs",
                "runs",
                "tasks",
                "branches",
                "repair_trial_jobs",
                "repair_trial_results",
                "object_refs",
            )
        }


def preflight(ledger, lease, service, worker):
    reserved = ledger.reserve(lease)
    identity = UUID(reserved["trial_id"])
    dispatched = service.dispatch(lease, identity)
    assert worker.run_once()["status"] == "completed"
    settled = service.settle(lease, identity)
    assert settled["result"]["verdict"] == "chain_valid"
    assert settled["result"]["usage"]["tokens"] == 0
    assert settled["result"]["assessment"] is None
    return identity, dispatched, settled


def test_recorded_preflight_then_real_plan_trial_preserves_source_and_charges_physical_usage(
    history,
    repair_case,
    monkeypatch,
):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case)
    original = history.branch(repo)
    source_id = UUID(repair_case[1].binding.failed_run_id)
    source_before = service.replays.research.history
    with history.engine.connect() as connection:
        source_row = dict(source_before._run(connection, repo, source_id))

    def forbidden(*args, **kwargs):
        pytest.fail("恢复证明和结算不能执行真实模型或工具")

    with monkeypatch.context() as readonly:
        readonly.setattr(MockResearchModel, "invoke", forbidden)
        readonly.setattr(ToolRegistry, "invoke", forbidden)
        identity, dispatched, recovered = preflight(ledger, lease, service, worker)
    recovery_report = history.objects.get_json(
        recovered["result"]["replay_report_ref"], ObjectType.BLOB
    )
    assert recovery_report["verified"] and recovery_report["logical_status"] == "failed"
    assert recovered["result"]["operation"] == "recorded_preflight"
    assert service.dispatch(lease, identity)["job_id"] == dispatched["job_id"]
    assert service.settle(lease, identity)["reused"]
    proposal = UUID(ledger.view(repo, search)["proposals"][2]["proposal_id"])
    selected = UUID(ledger.reserve(lease, proposal)["trial_id"])
    actual = service.dispatch(lease, selected)
    assert actual["initial_run_id"] != dispatched["initial_run_id"]
    assert worker.run_once()["status"] == "completed"
    result = service.settle(lease, selected)["result"]
    assert result["verdict"] == "passed" and result["determinate"]
    assert result["assessment"]["verdict"] == "passed" and result["test_only"]
    assert result["usage"]["model_calls"] > 0 and result["usage"]["tool_calls"] > 0
    assert result["usage"]["tokens"] > 0 and result["usage"]["cost_micro_usd"] == 0
    assert history.branch(repo) == original
    with history.engine.connect() as connection:
        assert dict(source_before._run(connection, repo, source_id)) == source_row
    view = ledger.view(repo, search)
    assert view["usage"]["active"] == 0 and view["usage"]["started"]["screen"] == 2
    assert view["usage"]["tokens"] == result["usage"]["tokens"]
    assert view["trials"][1]["result"]["verdict"] == "passed"
    assert worker.run_once() is None
    destination = os.environ.get("AGENTGIT_S9_TRIAL_EVIDENCE")
    if destination:
        with history.engine.connect() as connection:
            reference = connection.execute(
                sa.select(TABLES["runs"].c.id).where(
                    TABLES["runs"].c.repository_id == repo,
                    TABLES["runs"].c.branch_name == "main",
                )
            ).scalar_one()
            indexes = {
                name: [
                    public(dict(row))
                    for row in connection.execute(
                        sa.select(TABLES[name]).where(TABLES[name].c.repository_id == repo)
                    )
                    .mappings()
                    .all()
                ]
                for name in (
                    "repair_searches",
                    "repair_jobs",
                    "repair_proposals",
                    "repair_trials",
                    "repair_trial_jobs",
                    "repair_trial_results",
                    "repair_trial_waits",
                    "repair_events",
                    "jobs",
                    "replay_requests",
                )
            }
        evidence = {
            "repository_id": str(repo),
            "reference_run_id": str(reference),
            "failed_run_id": str(source_id),
            "case_id": "S9-atomic-trials/v1",
            "graph": BehaviorService(history).trace(repo, source_id).model_dump(mode="json"),
            "reference_graph": BehaviorService(history)
            .trace(repo, reference)
            .model_dump(mode="json"),
        }
        bundle = pack_repository_evidence(history, evidence)
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "kind": "repair-trial-evidence/v1",
                    "search": view,
                    "s9_indexes": indexes,
                    "evidence": evidence,
                    "raw_repository": bundle,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )


@pytest.mark.parametrize("point", ["after_job_created", "after_dispatch_commit"])
def test_dispatch_rollback_or_lost_acknowledgement_never_creates_a_second_job(
    history,
    repair_case,
    point,
):
    repo, search, ledger, lease, service, _ = setup_trials(history, repair_case)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    before = counts(history)

    def fail(stage):
        if stage == point:
            raise RuntimeError("模拟派发提交边界中断")

    service.fault = fail
    with pytest.raises(RuntimeError):
        service.dispatch(lease, identity)
    after = counts(history)
    if point == "after_job_created":
        assert after == before
    else:
        assert after["jobs"] == before["jobs"] + 1
    service.fault = lambda _: None
    dispatched = service.dispatch(lease, identity)
    assert counts(history)["jobs"] == before["jobs"] + 1
    assert service.dispatch(lease, identity)["job_id"] == dispatched["job_id"]
    assert counts(history)["repair_trial_jobs"] == 1


def test_cancel_during_preparation_prevents_any_new_job(history, repair_case, monkeypatch):
    repo, search, ledger, lease, service, _ = setup_trials(history, repair_case)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    before = counts(history)
    prepare = service.replays.prepare

    def cancel_after_prepare(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        ledger.cancel(repo, search, actor="operator")
        return prepared

    monkeypatch.setattr(service.replays, "prepare", cancel_after_prepare)
    with pytest.raises(AgentGitError) as error:
        service.dispatch(lease, identity)
    assert error.value.code == ErrorCode.CONTROL_REQUESTED
    after = counts(history)
    assert after["jobs"] == before["jobs"] and after["repair_trial_jobs"] == 0
    assert ledger.abandon_undispatched(lease) == 1
    assert ledger.view(repo, search)["status"] == "cancelled"


def test_dispatched_cancel_is_applied_by_worker_and_keeps_trial_count(history, repair_case):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    service.dispatch(lease, identity)
    ledger.cancel(repo, search, actor="operator")
    assert service.request_cancellation(lease) == 1
    assert service.request_cancellation(lease) == 0
    assert worker.run_once()["status"] == "cancelled"
    result = service.settle(lease, identity)["result"]
    assert result["category"] == "cancelled" and not result["determinate"]
    view = ledger.view(repo, search)
    assert view["status"] == "cancelled" and view["usage"]["active"] == 0
    assert view["usage"]["started"]["screen"] == 1


def test_missing_meter_retains_full_reservation_and_blocks_expansion_until_reconciled(
    history,
    repair_case,
    monkeypatch,
):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case)
    preflight(ledger, lease, service, worker)
    proposal = UUID(ledger.view(repo, search)["proposals"][0]["proposal_id"])
    identity = UUID(ledger.reserve(lease, proposal)["trial_id"])
    dispatched = service.dispatch(lease, identity)
    assert worker.run_once()["status"] == "failed"
    report = service.replays.report(repo, UUID(dispatched["job_id"]))
    unknown = deepcopy(report)
    unknown["actual"]["model_usage"]["mock"]["total_bytes"] = None
    with monkeypatch.context() as fault:
        fault.setattr(service.replays, "report", lambda *args: unknown)
        assert not service.settle(lease, identity)["settled"]
    view = ledger.view(repo, search)
    assert view["status"] == "waiting_reconciliation" and view["usage"]["active"] == 1
    with pytest.raises(AgentGitError) as error:
        ledger.reserve(lease, UUID(view["proposals"][2]["proposal_id"]))
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    result = service.settle(lease, identity)
    assert result["settled"] and result["result"]["verdict"] == "failed"
    assert result["result"]["category"] == "oracle"
    view = ledger.view(repo, search)
    assert view["status"] == "running" and view["usage"]["active"] == 0
    assert view["trials"][1]["waiting"]["reason"] == "physical_usage_unknown"
    assert view["usage"]["tokens"] == result["result"]["usage"]["tokens"]


@pytest.mark.parametrize("point", ["before_result_commit", "after_result_commit"])
def test_settlement_rollback_and_lost_acknowledgement_preserve_single_charge(
    history,
    repair_case,
    point,
):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    service.dispatch(lease, identity)
    assert worker.run_once()["status"] == "completed"

    def fail(stage):
        if stage == point:
            raise RuntimeError("模拟结算提交边界中断")

    service.fault = fail
    with pytest.raises(RuntimeError):
        service.settle(lease, identity)
    assert ledger.view(repo, search)["usage"]["active"] == (
        1 if point == "before_result_commit" else 0
    )
    service.fault = lambda _: None
    result = service.settle(lease, identity)
    assert result["settled"]
    assert counts(history)["repair_trial_results"] == 1
    assert service.settle(lease, identity)["result_ref"] == result["result_ref"]


def test_multiple_missing_meters_keep_search_blocked_until_all_are_settled(
    history,
    repair_case,
    monkeypatch,
):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case, concurrency=2)
    preflight(ledger, lease, service, worker)
    proposals = ledger.view(repo, search)["proposals"]
    identities = [
        UUID(ledger.reserve(lease, UUID(item["proposal_id"]))["trial_id"]) for item in proposals[:2]
    ]
    for identity in identities:
        service.dispatch(lease, identity)
    assert [worker.run_once()["status"] for _ in range(2)] == ["failed", "failed"]
    original = service.replays.report

    def unavailable(*args):
        report = deepcopy(original(*args))
        report["actual"]["model_usage"]["mock"]["total_bytes"] = None
        return report

    with monkeypatch.context() as fault:
        fault.setattr(service.replays, "report", unavailable)
        for identity in identities:
            assert not service.settle(lease, identity)["settled"]
    assert service.settle(lease, identities[0])["settled"]
    assert ledger.view(repo, search)["status"] == "waiting_reconciliation"
    with pytest.raises(AgentGitError):
        ledger.reserve(lease, UUID(proposals[2]["proposal_id"]))
    assert service.settle(lease, identities[1])["settled"]
    assert ledger.view(repo, search)["status"] == "running"
    assert ledger.reserve(lease, UUID(proposals[2]["proposal_id"]))["admitted"]


def test_stale_unknown_observation_cannot_reopen_an_already_settled_trial(
    history,
    repair_case,
    monkeypatch,
):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case)
    preflight(ledger, lease, service, worker)
    proposal = UUID(ledger.view(repo, search)["proposals"][0]["proposal_id"])
    identity = UUID(ledger.reserve(lease, proposal)["trial_id"])
    service.dispatch(lease, identity)
    assert worker.run_once()["status"] == "failed"
    concurrent = RepairTrialService(service.research)
    original = service.replays.report
    committed = []

    def obsolete_probe(*args):
        stale = deepcopy(original(*args))
        stale["actual"]["model_usage"]["mock"]["total_bytes"] = None
        committed.append(concurrent.settle(lease, identity))
        return stale

    with monkeypatch.context() as fault:
        fault.setattr(service.replays, "report", obsolete_probe)
        observed = service.settle(lease, identity)
    assert observed["settled"] and observed["reused"]
    assert observed["result_ref"] == committed[0]["result_ref"]
    view = ledger.view(repo, search)
    assert view["status"] == "running" and view["usage"]["active"] == 0
    assert "waiting" not in view["trials"][1]
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["repair_trial_waits"])
            ).scalar_one()
            == 0
        )


def test_single_pool_six_way_dispatch_reuses_one_job(history, repair_case):
    repo, search, ledger, lease, service, worker = setup_trials(history, repair_case)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    single = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        bounded = RepairTrialService(
            ResearchService(HistoryService(single, history.objects), history.objects.root.parent)
        )
        before = counts(history)
        with ThreadPoolExecutor(max_workers=6) as pool:
            submitted = list(pool.map(lambda _: bounded.dispatch(lease, identity), range(6)))
        assert len({item["job_id"] for item in submitted}) == 1
        assert counts(history)["jobs"] == before["jobs"] + 1
        assert worker.run_once()["status"] == "completed"
        assert bounded.settle(lease, identity)["result"]["verdict"] == "chain_valid"
    finally:
        single.dispose()


def test_recorded_wall_deadline_is_narrower_without_changing_recovery_configuration(
    history,
    repair_case,
):
    repo, search, ledger, lease, service, worker = setup_trials(
        history, repair_case, wall_ms=10_000
    )
    identity = UUID(ledger.reserve(lease)["trial_id"])
    dispatched = service.dispatch(lease, identity)
    queue = JobService(service.research)
    payload = queue.payload(queue.get(repo, UUID(dispatched["job_id"])))
    assert payload.max_wall_ms == 10_000
    assert payload.limits.max_wall_ms == 60_000
    assert worker.run_once()["status"] == "completed"
    assert service.settle(lease, identity)["result"]["verdict"] == "chain_valid"
    before = counts(history)
    binding = repair_case[1].binding
    with pytest.raises(AgentGitError) as error:
        service.replays.prepare(
            repo,
            RecordedReplay(
                source_run_id=binding.failed_run_id,
                from_commit=binding.base_commit,
                until_commit=ledger.view(repo, search)["manifest"]["source_head_commit"],
                branch_name="disallowed-wall-expansion",
            ),
            execution_wall_ms=60_001,
        )
    assert error.value.code == ErrorCode.FORBIDDEN
    assert counts(history) == before


def test_hybrid_tool_patch_owns_policy_and_keeps_synthetic_observation_test_only(
    history,
    replay_source,
):
    session, _, commits, _ = replay_source
    repo = session.repository_id
    manager = ManagementService(session.service)
    validator = ValidatorService(history).freeze(repo, session.state.goal, session.service.corpus)
    bad = evolve(
        session.state.plan,
        nodes=tuple(node for node in session.state.plan.nodes if node.kind != "write_sources"),
    )
    injected = manager.create_experiment(
        repo,
        NewExperiment(
            source_run_id=str(session.run_id),
            from_commit=commits[3],
            validator_ref=validator,
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="正常"),
                ExperimentGroup(key="B", label="遗漏来源产物", plan=bad),
            ),
        ),
        "hybrid-search-source",
    )
    worker = Worker(manager.jobs)
    assert sorted(worker.run_once()["status"] for _ in range(2)) == ["completed", "failed"]
    failed = next(
        item
        for item in manager.experiment(repo, UUID(injected["experiment_id"]))["trials"]
        if item["group"] == "B"
    )
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == UUID(failed["run_id"]),
                    TABLES["external_calls"].c.logical_step_id == "read-5",
                )
            )
            .mappings()
            .one()
        )
        event = (
            connection.execute(
                sa.select(TABLES["events"]).where(
                    TABLES["events"].c.run_id == UUID(failed["run_id"]),
                    TABLES["events"].c.logical_step_id == "read-5",
                    TABLES["events"].c.type == "tool.started",
                )
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    context = manager.intervention_context(
        repo, UUID(failed["run_id"]), intent.checkpoint, event["id"], validator
    )
    anchor = TemplateAnchor.model_validate_json(
        encode(
            {
                key: context[key]
                for key in (
                    "source_run_id",
                    "base_commit",
                    "target_commit_id",
                    "target_event_id",
                    "precondition",
                )
            }
        )
    )
    patch = observation_templates(anchor, (str(call["id"]),)).patches[0]
    published = manager.create_intervention(repo, patch.intervention, "synthetic-read-template")
    view = manager.checkout(repo, anchor.base_commit)
    binding = RepairBinding(
        repository_id=str(repo),
        failed_run_id=failed["run_id"],
        base_commit=anchor.base_commit,
        environment_ref=view["manifest"]["environment_ref"],
        validator_ref=validator,
    )
    space = freeze_space(binding, (patch,), source_reason="完整候选降级，检查观测派发路径")
    budget = evolve(
        RepairBudget(), trial=evolve(RepairBudget().trial, max_tokens=200_000), max_tokens=6_000_000
    )
    created = RepairSearchService(history).submit(
        space,
        budget,
        (published["intervention_id"],),
        actor="operator",
        key="hybrid-search",
        hybrid=HybridPolicy(until_commit=failed["checkpoint_commit_id"], calls=(str(call["id"]),)),
    )
    search = UUID(created["search_id"])
    ledger = RepairJobService(history, lease_ms=120_000)
    lease = ledger.claim("hybrid-controller")
    service = RepairTrialService(session.service)
    preflight(ledger, lease, service, worker)
    proposal = UUID(ledger.view(repo, search)["proposals"][0]["proposal_id"])
    selected = UUID(ledger.reserve(lease, proposal)["trial_id"])
    dispatched = service.dispatch(lease, selected)
    assert worker.run_once()["status"] == "failed"
    outcome = service.settle(lease, selected)["result"]
    assert outcome["job_id"] == dispatched["job_id"]
    assert outcome["test_only"] and outcome["verdict"] == "failed"
    report = history.objects.get_json(outcome["replay_report_ref"], ObjectType.BLOB)
    assert report["mode"] == "hybrid" and report["actual"]["synthetic_observations"] == 1
    assert report["actual"]["tool_calls"] == outcome["usage"]["tool_calls"]
    assert worker.run_once() is None
