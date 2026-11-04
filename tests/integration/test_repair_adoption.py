"""确切派生提案批准后只增加配置，新的真实任务再执行采纳的计划。"""

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from test_repair_confirmation import drive_confirmation, setup_confirmation
from test_repair_worker import ConditionalMock

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.repair_reviews import DecideRepairReview, RepairAdoptionProposal
from agentgit.core.research import ResearchGoal, evolve
from agentgit.core.versions import LiveReplay, RecordedReplay
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.repair_reviews import TABLES, RepairReviewService
from agentgit.worker import Worker

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
                "agent_configs",
                "proposal_reviews",
                "repair_saved_experiments",
            )
        }


def test_exact_field_plan_adoption_creates_one_config_and_real_new_task_uses_template(
    history, tmp_path, monkeypatch
):
    policy = "review-fixture-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    repo, study, research, source, reduced = setup_confirmation(
        history,
        tmp_path,
        mode="joint",
        policy=policy,
        field_policy="predefined-fields/v1",
        budget_transform=lambda b: evolve(b, quotas=evolve(b.quotas, reduce=16), max_trials=40),
    )
    result = drive_confirmation(history, research, repo, policy=policy)
    assert result["status"] == "verified_under_fixture"
    reviews = RepairReviewService(research)
    before = counts(history)
    branch = history.branch(repo)

    def forbidden(*args, **kwargs):
        pytest.fail("建议、审阅与批准不得调用模型、工具或提交 Job")

    with monkeypatch.context() as no_execution:
        no_execution.setattr(MockResearchModel, "invoke", forbidden)
        no_execution.setattr(ConditionalMock, "invoke", forbidden)
        no_execution.setattr(ToolRegistry, "invoke", forbidden)
        no_execution.setattr(JobService, "enqueue", forbidden)
        proposal = reviews.create(repo, study, actor="fixture", key="proposal")
        identity = UUID(proposal["proposal_id"])
        manifest = proposal["proposal"]
        assert manifest["changed_fields"] == ["prompt", "plan_template"]
        assert manifest["before"]["limits"] == manifest["after"]["limits"]
        assert manifest["after"]["prompt"] == result["trials"][0]["projection"]["config"]["prompt"]
        assert manifest["after"]["plan_template"] == result["trials"][0]["projection"]["plan"]
        assert "plan_template" not in manifest["before"]
        for changed in (
            {**manifest, "verification_ref": "f" * 64},
            {**manifest, "after": {**manifest["after"], "tools": ["artifact_write"]}},
            {**manifest, "source_goal": {**manifest["source_goal"], "text": "替换的适用任务"}},
        ):
            with pytest.raises(AgentGitError):
                parse_contract(encode(changed), RepairAdoptionProposal)
        assert reviews.create(repo, study, actor="fixture", key="proposal") == proposal
        ticket = reviews.begin(repo, identity, actor="fixture", key="review")
        request = DecideRepairReview(
            review_id=ticket["review_id"],
            proposal_ref=proposal["proposal_ref"],
            expected_config_version=ticket["ticket"]["expected_config_version"],
            decision="approve",
        )
        approved = reviews.decide(repo, identity, request, actor="fixture", key="approve")
        assert reviews.decide(repo, identity, request, actor="fixture", key="approve") == approved
    after = counts(history)
    assert after == {**before, "agent_configs": before["agent_configs"] + 1, "proposal_reviews": 1}
    assert history.branch(repo) == branch
    config = UUID(approved["decision"]["new_config_id"])
    assert research.config(repo, config).model_dump(mode="json") == manifest["after"]
    assert (
        research.config(repo, UUID(manifest["source_config_id"])).model_dump(mode="json")
        == manifest["before"]
    )
    adapter = hashlib.sha256(
        Path(__file__).with_name("test_repair_worker.py").read_bytes()
    ).hexdigest()
    goal = ResearchGoal(text=f"joint;adapter_sha256={adapter};approved-template-new-task")
    task = research.create_task(repo, config, goal)
    run = research.start(repo, task)
    finished = run.execute(ConditionalMock())
    assert finished.status.value == "completed" and finished.usage.replans == 0
    assert finished.plan.model_dump(mode="json") == manifest["after"]["plan_template"]
    with history.engine.connect() as connection:
        links = TABLES["run_commits"]
        commits = (
            connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == run.run_id)
                .order_by(links.c.run_order)
            )
            .scalars()
            .all()
        )
        events = TABLES["events"]
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(events)
                .where(events.c.run_id == run.run_id, events.c.type == "plan.accepted")
            ).scalar_one()
            == 1
        )
    seeded = history.restore(repo, commits[1])
    assert (
        seeded.data["completed"] == []
        and not seeded.data["working_memory"]
        and not seeded.data["messages"]
    )
    assert not seeded.artifacts and seeded.data["plan_version"] == 1
    manager = ManagementService(research)
    recorded = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=str(run.run_id),
            from_commit=commits[0],
            until_commit=run.head,
            branch_name="approved-recorded",
        ),
        "recorded-template",
    )
    with monkeypatch.context() as recorded_only:
        recorded_only.setattr(MockResearchModel, "invoke", forbidden)
        recorded_only.setattr(ToolRegistry, "invoke", forbidden)
        assert Worker(manager.jobs).run_once()["status"] == "completed"
    assert manager.replay_status(repo, UUID(recorded["job_id"]))["replay"]["verified"]
    for number, checkpoint in enumerate(commits[:2]):
        live = manager.replay(
            repo,
            LiveReplay(
                source_run_id=str(run.run_id),
                from_commit=checkpoint,
                branch_name=f"approved-live-{number}",
            ),
            f"live-template-{number}",
        )
        assert (
            Worker(manager.jobs, model_factory=ConditionalMock).run_once()["status"] == "completed"
        )
        assert (
            manager.replay_status(repo, UUID(live["job_id"]))["replay"]["actual"]["model_calls"] > 0
        )
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-reviewed-field-plan-real-task/v1",
        environment="AGENTGIT_S10_ADOPTION_EVIDENCE",
    )
