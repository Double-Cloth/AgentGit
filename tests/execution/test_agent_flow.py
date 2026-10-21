"""实际 Agent/Worker 五工具闭环，不用 Mock 执行器替代 Shell/Python。"""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters import mock
from agentgit.adapters.execution.policy import bundled_policy
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.execution import ExecutionResult
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetLimits, ResearchGoal
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


def submit(history, tmp_path, *, tools=("code_exec", "terminal_exec"), limits=None):
    research = ResearchService(history, tmp_path / "five-tools")
    repository = history.create_repository("实际五工具研究")
    options = {} if limits is None else {"limits": limits}
    config = AgentConfig(
        tools=(*AgentConfig().tools, *tools), execution_policy=bundled_policy(), **options
    )
    config_id = research.create_config(repository, config)
    task = research.create_task(
        repository, config_id, ResearchGoal(text="比较三个合成框架并计算统计、整理产物")
    )
    queue = JobService(research)
    job = queue.enqueue(
        repository,
        task,
        actor="local-operator",
        key="five-tools",
        config_version=1,
        expected_head=None,
        expected_branch_version=0,
    )
    return queue, repository, job


def final(queue, repository, job):
    current = queue.get(repository, UUID(job.job_id))
    run = queue.run(repository, current["run_id"])
    snapshot = queue.history.restore(repository, run["checkpoint_commit_id"])
    state = parse_contract(encode(snapshot.data), AgentState)
    files = {
        item.path: queue.history.objects.get(item.object_ref, ObjectType.BLOB)
        for item in snapshot.artifacts
    }
    return state, files, snapshot


def test_worker_completes_five_real_tools_and_promotes_only_complete_manifests(
    history, tmp_path, image_id
):
    queue, repository, job = submit(history, tmp_path)
    result = Worker(queue).run_once()
    assert result["status"] == "completed", result
    state, files, snapshot = final(queue, repository, job)
    assert state.status == RunStatus.COMPLETED and state.evaluation.passed
    assert state.usage.tool_calls == 13 and state.usage.model_calls == 15
    assert set(files) == {
        "report.md",
        "sources.json",
        "execution/statistics.py",
        "execution/organize.sh",
        "results/statistics.json",
        "results/report.md",
        "results/execution.txt",
    }
    assert files["results/report.md"] == files["report.md"]
    assert json.loads(files["results/statistics.json"]) == {
        "schema_version": 1,
        "frameworks": 3,
        "claims": 9,
        "sources": 6,
        "claims_per_framework": {"Aster": 3, "Birch": 3, "Cedar": 3},
    }
    assert {item.path: item.media_type for item in snapshot.artifacts}[
        "report.md"
    ] == "text/markdown"
    assert {item.path: item.media_type for item in snapshot.artifacts}[
        "results/statistics.json"
    ] == "application/json"
    with history.engine.connect() as connection:
        executions = list(connection.execute(sa.select(TABLES["sandbox_executions"])).mappings())
    assert len(executions) == 2
    for row in executions:
        saved = parse_contract(
            history.objects.get(row["response_ref"], ObjectType.BLOB), ExecutionResult
        )
        assert row["status"] == "complete" and saved.error_code is None
        assert saved.image_id == image_id and saved.cleanup_confirmed and saved.integrity_complete
    assert {trace.provider for trace in state.messages} >= {
        "tool:corpus_search/v1",
        "tool:source_read/v1",
        "tool:artifact_write/v1",
        "tool:code_exec/v1",
        "tool:terminal_exec/v1",
    }


@pytest.mark.parametrize("tools", [("code_exec",), ("terminal_exec",)])
def test_single_execution_tool_permission_does_not_enable_the_other(
    history, tmp_path, image_id, tools
):
    queue, repository, job = submit(history, tmp_path, tools=tools)
    result = Worker(queue).run_once()
    assert result["status"] == "completed", result
    state, _, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 11
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("control", [None, "cancel", "deadline"])
def test_saved_execution_result_is_settled_before_recovery_control(
    history, tmp_path, image_id, control
):
    queue, repository, job = submit(history, tmp_path)
    crashed = []

    def fault(point):
        if point != "after_result" or crashed:
            return
        with history.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["sandbox_executions"])
                .where(TABLES["sandbox_executions"].c.status == "complete")
            ).scalar_one()
        if count == 1:
            crashed.append(True)
            raise RuntimeError("执行结果保存后、观察前崩溃")

    with pytest.raises(RuntimeError, match="观察前崩溃"):
        Worker(queue, fault=fault).run_once()
    before = queue.run(repository, UUID(job.run_id))
    if control == "cancel":
        queue.request_control(
            repository, UUID(job.run_id), "cancel", expected_version=before["state_version"]
        )
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(job.job_id))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
        if control == "deadline":
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == UUID(job.job_id))
                .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
    summary = Worker(queue).run_once()
    state, files, _ = final(queue, repository, job)
    expected = {None: "completed", "cancel": "cancelled", "deadline": "budget_exhausted"}
    assert summary["status"] == expected[control], summary
    assert state.usage.tool_calls == (11 if control else 13)
    with history.engine.connect() as connection:
        count = connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
        ).scalar_one()
    assert count == (1 if control else 2)
    assert "statistics.json" in files if control else "results/statistics.json" in files
    assert (
        queue.run(repository, UUID(job.run_id))["checkpoint_commit_id"]
        == before["checkpoint_commit_id"]
    )


def test_tool_budget_stops_before_second_container_and_keeps_first_output(
    history, tmp_path, image_id
):
    queue, repository, job = submit(history, tmp_path, limits=BudgetLimits(max_tool_calls=12))
    summary = Worker(queue).run_once()
    assert summary["status"] == "budget_exhausted", summary
    state, files, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 12
    assert "statistics.json" in files and "results/statistics.json" not in files
    assert "execution/organize.sh" in files
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize(
    ("program", "error"),
    [
        (
            "from pathlib import Path; Path('partial.txt').write_text('partial'); "
            "raise RuntimeError('bad')",
            "execution_failed",
        ),
        ("from pathlib import Path; Path('link.txt').symlink_to('report.md')", "export_invalid"),
        ("from pathlib import Path; Path('large.txt').write_bytes(b'x'*70000)", "output_limit"),
    ],
)
def test_failed_or_over_budget_execution_never_promotes_partial_workspace(
    history, tmp_path, image_id, monkeypatch, program, error
):
    monkeypatch.setattr(mock, "_STATISTICS_CODE", program)
    queue, repository, job = submit(history, tmp_path)
    summary = Worker(queue).run_once()
    assert summary["status"] == "failed", summary
    state, files, _ = final(queue, repository, job)
    assert state.error_code == error and state.usage.tool_calls == 11
    assert set(files) == {"report.md", "sources.json", "execution/statistics.py"}
    assert "code_exec" not in state.completed
    with history.engine.connect() as connection:
        executions = list(connection.execute(sa.select(TABLES["sandbox_executions"])).mappings())
    assert len(executions) == 1 and executions[0]["status"] == "complete"
    saved = parse_contract(
        history.objects.get(executions[0]["response_ref"], ObjectType.BLOB), ExecutionResult
    )
    assert saved.cleanup_confirmed
    # 工作区的业务预算比容器导出硬上限更小；已录制的完整输出仍不能越过业务预算。
    assert saved.error_code == (None if error == "output_limit" else error)
