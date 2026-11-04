"""工程研究用例复用耐久控制器，测试驱动只推进真实 Job。"""

from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import encode
from agentgit.core.repair_pools import NewRepairValidationSuite
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ResearchGoal
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.jobs import JobService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_studies import TABLES, RepairStudyService
from agentgit.worker import Worker


def register_suite(research, repo, search, config, binding):
    origins = []
    for index in range(4):
        task = research.create_task(
            repo,
            config,
            ResearchGoal(text=f"regression-{index};{binding};fixed-semantic-regression"),
        )
        run = research.start(repo, task)
        assert run.execute(MockResearchModel()).status.value == "completed"
        with research.history.engine.connect() as connection:
            links = TABLES["run_commits"]
            first = connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == run.run_id)
                .order_by(links.c.run_order)
                .limit(1)
            ).scalar_one()
        origins.append(dict(schema_version=1, source_run_id=str(run.run_id), from_commit=first))
    return RepairValidationPoolService(research).submit(
        repo,
        NewRepairValidationSuite.model_validate_json(
            encode(dict(schema_version=1, search_id=str(search), regression=origins))
        ),
        actor="fixture",
        key="registered-before-screen",
    )


def drive(research, repo, identity, worker, phase):
    if phase == "screen":
        controller = RepairSearchWorker(research)
        terminal = (
            "needs_confirmation",
            "no_solution_in_space",
            "not_found",
            "budget_exhausted",
            "needs_attention",
            "cancelled",
        )
    elif phase == "reduce":
        controller = RepairStudyWorker(research)
        terminal = (
            "awaiting_samples",
            "needs_review",
            "budget_exhausted",
            "needs_attention",
            "cancelled",
        )
    else:
        assert phase == "confirm"
        controller = RepairConfirmationWorker(research)
        terminal = (
            "verified_under_fixture",
            "empirically_confirmed",
            "rejected",
            "needs_review",
            "needs_attention",
            "cancelled",
        )
    for _ in range(70):
        current = controller.run_once(repository=repo)
        assert current is not None
        if current["status"] in terminal:
            assert worker.run_once() is None
            assert current["search_id" if phase == "screen" else "study_id"] == str(identity)
            return current
        assert current["status"] not in ("waiting_reconciliation", "cancelling"), current
        worker.run_once()
    pytest.fail("预先声明的有限研究流程没有终止")


def reduce_and_open(research, repo, search, suite, worker, *, field_policy="predefined-fields/v1"):
    made = RepairStudyService(research.history).submit(
        repo,
        NewRepairStudy(search_id=str(search), field_policy=field_policy),
        actor="fixture",
        key="study",
    )
    study = UUID(made["study_id"])
    reduced = drive(research, repo, study, worker, "reduce")
    assert reduced["status"] == "awaiting_samples", reduced
    assert RepairValidationPoolService(research).open(
        repo, study, UUID(suite["suite_id"]), actor="fixture", key="open"
    )["opened"]
    return study, reduced


def worker(research, model_factory):
    return Worker(JobService(research), model_factory=model_factory)
