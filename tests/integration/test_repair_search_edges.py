"""自动控制器的并发排空、多维准入和真实进程中断边界。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_repair_worker import ConditionalMock, create_case

from agentgit.core.research import evolve
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.jobs import JobService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "dimension,reason",
    [
        ("max_tokens", "tokens"),
        ("max_model_calls", "model_calls"),
        ("max_tool_calls", "tool_calls"),
        ("max_wall_ms", "wall_time"),
    ],
)
def test_controller_reports_resource_stop_without_starting_trial(
    history, tmp_path, dimension, reason
):
    def insufficient(budget):
        trial = budget.trial
        amount = 10 * getattr(trial, dimension) + getattr(trial, dimension) - 1
        return evolve(budget, **{dimension: amount})

    repo, search, research, _ = create_case(history, tmp_path, budget_transform=insufficient)
    result = RepairSearchWorker(research).run_once(repository=repo)
    assert result["status"] == "budget_exhausted" and result["stop_reason"] == reason
    assert result["trials"] == []
    assert result["decision"]["explored_proposals"] == 0
    assert result["decision"]["unexplored_proposals"] == 2
    assert Worker(JobService(research)).run_once() is None


def test_passing_candidate_drains_other_inflight_trial_before_sealing(history, tmp_path):
    repo, search, research, _ = create_case(
        history,
        tmp_path,
        budget_transform=lambda b: evolve(b, max_concurrency=2),
    )
    runner, worker = (
        RepairSearchWorker(research),
        Worker(JobService(research), model_factory=ConditionalMock),
    )
    assert len(runner.run_once(repository=repo)["trials"]) == 1
    assert worker.run_once()["status"] == "completed"
    initial = runner.run_once(repository=repo)
    assert initial["usage"]["active"] == 2 and len(initial["trials"]) == 3
    assert worker.run_once()["status"] == "completed"
    waiting = runner.run_once(repository=repo)
    assert waiting["status"] == "running" and waiting["usage"]["active"] == 1
    assert len(waiting["trials"]) == 3 and "decision" not in waiting
    assert worker.run_once()["status"] == "failed"
    result = runner.run_once(repository=repo)
    assert result["status"] == "needs_confirmation" and result["usage"]["active"] == 0
    assert result["decision"]["explored_proposals"] == 2
    assert result["decision"]["passed_proposals"] == 1


def test_actual_controller_process_exit_after_dispatch_reuses_original_job(history, tmp_path):
    repo, search, research, _ = create_case(history, tmp_path)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("repair_search_process.py")),
            schema,
            str(history.objects.root),
            str(research.data_root),
            str(repo),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stdout + child.stderr
    ledger = RepairJobService(history)
    dispatched = ledger.view(repo, search)["trials"][0]
    # 子进程在提交后停留至短租约到期再退出；新进程只能复用原意图/Job。
    resumed = RepairSearchWorker(research).run_once(repository=repo)
    assert resumed["trials"] == [dispatched]
    assert (
        Worker(JobService(research), model_factory=ConditionalMock).run_once()["status"]
        == "completed"
    )
    assert len(RepairSearchWorker(research).run_once(repository=repo)["trials"]) == 2
    ledger.cancel(repo, search, actor="fixture-cleanup")
    RepairSearchWorker(research).run_once(repository=repo)
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    assert RepairSearchWorker(research).run_once(repository=repo)["status"] == "cancelled"
