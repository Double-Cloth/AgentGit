"""预先冻结 Bernoulli 伪随机种子向量；偶然筛选通过不能顶替六次确认。"""

import hashlib
import json
import random
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from repair_case_execution import drive, reduce_and_open, register_suite, worker
from test_repair_worker import create_case

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairSearchManifest
from agentgit.core.repair_reviews import DecideRepairReview
from agentgit.core.research import ModelRequest, ToolDecision, evolve, parse_decision
from agentgit.services.jobs import JobService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_reviews import TABLES, RepairReviewService
from agentgit.services.repair_searches import RepairSearchService

pytestmark = pytest.mark.integration
SEEDS = (31, 0, 1, 2, 3, 4, 5, 6)
POLICY = (
    "seeded-bernoulli-artifact/v1;p=1/10;seeds=31,0,1,2,3,4,5,6;"
    "assignment=source-goal-job-publication-order/v1"
)


def binding():
    return (
        POLICY
        + ";stochastic-adapter-sha256="
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )


class SeededArtifactModel(MockResearchModel):
    """不读取阶段、补丁标签或验收；一次 Job 的样本由预注册顺序稳定分配。"""

    def __init__(self, seed):
        self.draw = random.Random(seed).random() if seed is not None else None

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert binding() in request.state.goal.text
        decision = parse_decision(super().invoke(payload))
        if (
            request.state.goal.text.startswith("stochastic-occasional;")
            and isinstance(decision, ToolDecision)
            and decision.arguments.get("path") == "sources.json"
        ):
            has_rule = "仅引用本次已读取" in request.prompt
            assert self.draw is not None
            if not has_rule or self.draw >= 0.1:
                decision = evolve(
                    decision, arguments={**decision.arguments, "path": "fixture-sources.json"}
                )
        return encode(decision.model_dump(mode="json"))


def seeded_factory(research, repo, source_goal_ref, samples):
    def factory():
        with research.history.engine.connect() as connection:
            jobs = (
                connection.execute(
                    sa.select(TABLES["jobs"])
                    .where(TABLES["jobs"].c.repository_id == repo)
                    .order_by(TABLES["jobs"].c.created_at, TABLES["jobs"].c.id)
                )
                .mappings()
                .all()
            )
        (running,) = [row for row in jobs if row["status"] == "running"]
        queue = JobService(research)
        payload = queue.payload(dict(running))
        if payload.goal_ref != source_goal_ref:
            return SeededArtifactModel(None)
        eligible = [
            row
            for row in jobs
            if (p := queue.payload(dict(row))).goal_ref == source_goal_ref
            and p.replay is not None
            and p.replay.mode != "recorded"
        ]
        ordinal = next(i for i, row in enumerate(eligible) if row["id"] == running["id"])
        assert ordinal < len(SEEDS), "不能在看到确认失败后追加种子或候选"
        seed = SEEDS[ordinal]
        sample = dict(
            job_id=str(running["id"]),
            ordinal=ordinal,
            seed=seed,
            draw=repr(random.Random(seed).random()),
        )
        if ordinal in samples:
            assert samples[ordinal] == sample
        samples[ordinal] = sample
        return SeededArtifactModel(seed)

    return factory


def test_seeded_rare_screen_pass_then_six_confirmation_failures_stays_rejected(history, tmp_path):
    repo, search, research, failed = create_case(
        history,
        tmp_path,
        mode="stochastic-occasional",
        adapter_binding=binding(),
        fixture_scope="stochastic_fixture",
    )
    source_queued = RepairJobService(history).view(repo, search)
    manifest = RepairSearchManifest.model_validate_json(encode(source_queued["manifest"]))
    assert manifest.fixture_scope == "stochastic_fixture" and not source_queued["trials"]
    with history.engine.connect() as connection:
        source_run = history._run(connection, repo, failed.run_id)
        task = (
            connection.execute(
                sa.select(TABLES["tasks"]).where(TABLES["tasks"].c.id == source_run["task_id"])
            )
            .mappings()
            .one()
        )
    suite = register_suite(research, repo, search, source_run["agent_config_id"], binding())
    # 同一原请求不能通过改参数复用已登记的搜索身份。
    with pytest.raises(AgentGitError) as error:
        RepairSearchService(history).submit(
            manifest.space,
            manifest.budget,
            manifest.intervention_ids,
            actor="test-operator",
            key="automatic-search",
        )
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    samples = {}
    actual = worker(research, seeded_factory(research, repo, task["goal_ref"], samples))
    source = drive(research, repo, search, actual, "screen")
    assert (
        source["status"] == "needs_confirmation"
        and source["trials"][-1]["result"]["verdict"] == "passed"
    )
    selected = source["decision"]["selected_result_ref"]
    study, reduced = reduce_and_open(research, repo, search, suite, actual, field_policy=None)
    assert reduced["final_lock"]["lock"]["candidate"]["model_scope"] == "stochastic_fixture"
    assert (
        reduced["reduction"]["minimality"] == "empirical"
        and reduced["trials"][0]["result"]["verdict"] == "failed"
    )
    result = drive(research, repo, study, actual, "confirm")
    assert result["status"] == "rejected", result
    summary = result["verification"]
    assert (
        summary["confirm_attempted"] == summary["confirm_determinate"] == 6
        and summary["confirm_passed"] == 0
    )
    assert summary["regression_passed"] == 4 and not summary["production_eligible"]
    assert (
        "confirmation_threshold_failed" in summary["reasons"]
        and selected not in summary["result_refs"]
    )
    assert len(samples) == len(SEEDS) and samples[0]["draw"] == repr(random.Random(31).random())
    assert float(samples[0]["draw"]) < 0.1 and all(
        float(samples[i]["draw"]) >= 0.1 for i in range(1, len(SEEDS))
    )
    source_jobs = {trial["dispatch"]["job_id"] for trial in source["trials"] if "dispatch" in trial}
    assert all(
        trial["result"]["job_id"] not in source_jobs and trial["result"]["fresh_model_calls"] > 0
        for trial in result["trials"]
    )
    original_opening = RepairValidationPoolService(research).open(
        repo, study, UUID(suite["suite_id"]), actor="fixture", key="cannot-reopen-failed-pool"
    )
    assert original_opening["plan_ref"] == result["plan_ref"]
    from agentgit.repair.confirmation_worker import RepairConfirmationWorker

    assert RepairConfirmationWorker(research).run_once(repository=repo) is None
    assert actual.run_once() is None
    reviews = RepairReviewService(research)
    proposal = reviews.create(repo, study, actor="fixture", key="proposal")
    identity = UUID(proposal["proposal_id"])
    ticket = reviews.begin(repo, identity, actor="fixture", key="review")
    request = DecideRepairReview(
        review_id=ticket["review_id"],
        proposal_ref=proposal["proposal_ref"],
        expected_config_version=ticket["ticket"]["expected_config_version"],
        decision="approve",
    )
    with pytest.raises(AgentGitError) as error:
        reviews.decide(repo, identity, request, actor="fixture", key="cannot-adopt")
    assert error.value.code == ErrorCode.FORBIDDEN
    assert RepairJobService(history).view(repo, search)["decision"] == source["decision"]
    bundle = capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-T02-seeded-stochastic-gate/v1",
        environment="AGENTGIT_S10_STOCHASTIC_GATE_EVIDENCE",
    )
    # 种子在首次试验前声明；实际 Job 的分配另留可复核侧表，不代替原始响应。
    import os

    output = os.environ.get("AGENTGIT_S10_STOCHASTIC_SEEDS_EVIDENCE")
    if output:
        with Path(output).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(
                    schema_version=1,
                    policy=POLICY,
                    binding=binding(),
                    source_goal_ref=task["goal_ref"],
                    samples=[samples[i] for i in range(len(SEEDS))],
                    archive_case=bundle["case_id"],
                ),
                handle,
                ensure_ascii=False,
                indent=2,
            )
