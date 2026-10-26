"""冻结适配器和响应策略后的实际六次确认/四次回归，不使用付费模型。"""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_repair_studies import drive_study
from test_repair_worker import ConditionalMock, create_case, drive

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import encode
from agentgit.core.models import parse_contract
from agentgit.core.repair_pools import NewRepairValidationSuite
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ModelRequest, ResearchGoal, ToolDecision, evolve, parse_decision
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.services.jobs import JobService
from agentgit.services.repair_confirmation import TABLES, RepairConfirmationService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def adapter_binding(policy):
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return f"confirmation_fixture_sha256={digest};confirmation_policy={policy}"


class ConfirmationMock(MockResearchModel):
    """按预先冻结策略生成真实新响应；固定失败向量不冒充随机分布评估。"""

    def __init__(self, policy="pass", *, fail=False):
        self.policy, self.fail = policy, fail

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert adapter_binding(self.policy) in request.state.goal.text
        raw = (
            ConditionalMock().invoke(payload)
            if request.state.goal.text.startswith(("prompt;", "joint;"))
            else super().invoke(payload)
        )
        result = parse_decision(raw)
        if (
            self.fail
            and isinstance(result, ToolDecision)
            and result.arguments.get("path") == "sources.json"
        ):
            result = evolve(
                result, arguments={**result.arguments, "path": "unconfirmed-sources.json"}
            )
        return encode(result.model_dump(mode="json"))


def setup_confirmation(
    history,
    tmp_path,
    *,
    policy="pass",
    mode="prompt",
    budget_transform=lambda value: value,
    field_policy=None,
):
    repo, search, research, _ = create_case(
        history,
        tmp_path,
        mode=mode,
        adapter_binding=adapter_binding(policy),
        budget_transform=budget_transform,
    )
    config, origins = research.create_config(repo), []
    for index in range(4):
        task = research.create_task(
            repo,
            config,
            ResearchGoal(text=f"regression-{index};{adapter_binding(policy)};固定研究任务"),
        )
        session = research.start(repo, task)
        assert session.execute(ConfirmationMock(policy)).status.value == "completed"
        with history.engine.connect() as connection:
            links = TABLES["run_commits"]
            first = connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == session.run_id)
                .order_by(links.c.run_order)
                .limit(1)
            ).scalar_one()
        origins.append(dict(schema_version=1, source_run_id=str(session.run_id), from_commit=first))
    pools = RepairValidationPoolService(research)
    registered = pools.submit(
        repo,
        NewRepairValidationSuite.model_validate_json(
            encode(dict(schema_version=1, search_id=str(search), regression=origins))
        ),
        actor="operator",
        key="suite",
    )
    source = drive(research, repo, search)
    assert source["status"] == "needs_confirmation"
    made = RepairStudyService(history).submit(
        repo,
        NewRepairStudy(search_id=str(search), field_policy=field_policy),
        actor="operator",
        key="study",
    )
    study = UUID(made["study_id"])
    reduced = drive_study(research, repo)
    assert pools.open(repo, study, UUID(registered["suite_id"]), actor="operator", key="open")[
        "opened"
    ]
    return repo, study, research, source, reduced


def actual_worker(history, research, repo, policy):
    def factory():
        with history.engine.connect() as connection:
            trials, jobs, dispatch = (
                TABLES["repair_validation_trials"],
                TABLES["jobs"],
                TABLES["repair_validation_jobs"],
            )
            row = (
                connection.execute(
                    sa.select(trials.c.phase, trials.c.ordinal)
                    .join(dispatch, dispatch.c.id == trials.c.id)
                    .join(jobs, jobs.c.id == dispatch.c.job_id)
                    .where(jobs.c.repository_id == repo, jobs.c.status == "running")
                )
                .mappings()
                .one()
            )
        fail = (
            (policy == "one_confirm_failure" and row["ordinal"] == 0)
            or (policy == "two_confirm_failures" and row["ordinal"] < 2)
            or (policy == "regression_failure" and row["ordinal"] == 6)
        )
        return ConfirmationMock(policy, fail=fail)

    return Worker(JobService(research), model_factory=factory)


def drive_confirmation(history, research, repo, *, policy="pass"):
    runner, worker = (
        RepairConfirmationWorker(research),
        actual_worker(history, research, repo, policy),
    )
    for _ in range(30):
        result = runner.run_once(repository=repo)
        assert result is not None
        if result["status"] in (
            "verified_under_fixture",
            "empirically_confirmed",
            "rejected",
            "needs_review",
            "needs_attention",
            "cancelled",
        ):
            assert worker.run_once() is None
            return result
        assert result["status"] in ("confirming", "regressing")
        assert worker.run_once() is not None
    pytest.fail("确认/回归未在十张冻结票据内结束")


def test_ten_new_jobs_confirm_and_regress_exact_locked_candidate_and_freeze_summary(
    history, tmp_path
):
    repo, study, research, source, reduced = setup_confirmation(history, tmp_path)
    branch = history.branch(repo)
    result = drive_confirmation(history, research, repo)
    assert result["status"] == "verified_under_fixture", result
    summary = result["verification"]
    assert summary["confirm_passed"] == summary["confirm_attempted"] == 6
    assert summary["regression_passed"] == summary["regression_attempted"] == 4
    assert summary["adoption"] == "not_reviewed" and summary["production_eligible"]
    assert len(result["trials"]) == 10 and result["usage"]["active"] == 0
    jobs = {trial["result"]["job_id"] for trial in result["trials"]}
    runs = {trial["result"]["current_run_id"] for trial in result["trials"]}
    assert len(jobs) == len(runs) == 10
    assert runs.isdisjoint(reduced["final_lock"]["lock"]["prior_run_ids"])
    assert all(trial["result"]["fresh_model_calls"] > 0 for trial in result["trials"])
    assert all(
        trial["result"]["candidate_ref"] == reduced["final_lock"]["lock_ref"]
        for trial in result["trials"]
    )
    for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd"):
        assert result["usage"][name] == reduced["usage"][name] + sum(
            trial["result"]["usage"][name] for trial in result["trials"]
        )
    assert (
        history.branch(repo) == branch
        and RepairStudyService(history).view(repo, study)["final_lock"] == reduced["final_lock"]
    )
    assert RepairConfirmationWorker(research).run_once(repository=repo) is None
    assert RepairConfirmationService(research).view(repo, study)["verification"] == summary
    path = os.environ.get("AGENTGIT_S10_CONFIRMATION_EVIDENCE")
    if path:
        from diagnosis_evidence import pack_repository_evidence

        from agentgit.services.diagnosis import BehaviorService
        from agentgit.services.management import public

        source_run = UUID(source["manifest"]["space"]["binding"]["failed_run_id"])
        graph = BehaviorService(history).trace(repo, source_run).model_dump(mode="json")
        evidence = dict(
            repository_id=str(repo),
            failed_run_id=str(source_run),
            reference_run_id=str(source_run),
            reference_role="source_alias_for_archive_only",
            case_id="S10-confirm-six-regress-four/v1",
            graph=graph,
            reference_graph=graph,
        )
        raw = pack_repository_evidence(history, evidence)
        with history.engine.connect() as connection:
            indexes = {
                name: [
                    public(dict(row))
                    for row in connection.execute(sa.select(TABLES[name])).mappings()
                ]
                for name in (
                    "repair_validation_trials",
                    "repair_validation_jobs",
                    "repair_validation_results",
                    "repair_validation_summaries",
                    "repair_study_events",
                    "jobs",
                    "replay_requests",
                )
            }
        with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(
                    schema_version=1,
                    result=result,
                    reduced=reduced,
                    source=source,
                    indexes=indexes,
                    evidence=evidence,
                    raw_repository=raw,
                ),
                handle,
                ensure_ascii=False,
                indent=2,
            )
