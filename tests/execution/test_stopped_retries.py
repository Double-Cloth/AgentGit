"""丢失完整结果后，只从原输入创建有独立预算的新容器尝试。"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_agent_flow import final, submit

from agentgit.adapters import mock
from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import EngineError
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallOccurrence
from agentgit.core.research import BudgetLimits
from agentgit.services.executions import ExecutionService
from agentgit.services.history import TABLES
from agentgit.services.managed_calls import ManagedCallContext
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


def expire(queue, job, *, deadline=False):
    values = {"lease_until": sa.func.clock_timestamp() - sa.text("interval '1 second'")}
    if deadline:
        values["deadline_at"] = sa.func.clock_timestamp() - sa.text("interval '1 second'")
    with queue.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"].update().where(TABLES["jobs"].c.id == UUID(job.job_id)).values(**values)
        )


def crash_after_execution(point):
    if point == "sandbox_result_transaction":
        raise RuntimeError("执行结果事务崩溃")


def executions(history):
    with history.engine.connect() as connection:
        return list(connection.execute(sa.select(TABLES["sandbox_executions"])).mappings())


def make_cleanup_available(queue, job):
    with queue.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(job.job_id))
            .values(available_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )


def test_missing_result_retries_same_input_in_fresh_container_with_separate_budget(
    history, tmp_path, image_id, monkeypatch
):
    monkeypatch.setattr(
        mock,
        "_STATISTICS_CODE",
        mock._STATISTICS_CODE + "\nfrom pathlib import Path\n"
        "counter = Path('attempt-count.txt')\n"
        "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n",
    )
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    original = queue.run(repository, UUID(job.run_id))["checkpoint_commit_id"]
    expire(queue, job)
    summary = Worker(queue).run_once()
    assert summary["status"] == "completed", summary
    state, files, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 14 and state.usage.model_calls == 15
    assert state.attempts["code_exec"] == 2
    assert len(state.stopped_executions) == 1
    assert files["attempt-count.txt"] == b"1"
    rows = executions(history)
    assert len(rows) == 3
    old = next(row for row in rows if row["run_id"] == UUID(job.run_id))
    retry = next(row for row in rows if row["attempt"] == 2)
    assert old["cleanup_confirmed"] and old["result_ref"] is None
    assert old["input_manifest_ref"] == retry["input_manifest_ref"]
    assert old["request_digest"] == retry["request_digest"]
    assert old["container_name"] != retry["container_name"]
    assert queue.run(repository, UUID(job.run_id))["checkpoint_commit_id"] == original


@pytest.mark.parametrize("stop", ["cancel", "deadline", "tool_budget"])
def test_stopped_attempt_is_charged_before_control_or_budget_prevents_new_container(
    history, tmp_path, image_id, stop
):
    limits = BudgetLimits(max_tool_calls=11) if stop == "tool_budget" else None
    queue, repository, job = submit(history, tmp_path, limits=limits)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    if stop == "cancel":
        run = queue.run(repository, UUID(job.run_id))
        queue.request_control(
            repository, UUID(job.run_id), "cancel", expected_version=run["state_version"]
        )
    expire(queue, job, deadline=stop == "deadline")
    summary = Worker(queue).run_once()
    assert summary["status"] == ("cancelled" if stop == "cancel" else "budget_exhausted"), summary
    state, files, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 11 and len(state.stopped_executions) == 1
    assert "statistics.json" not in files
    assert len(executions(history)) == 1


def test_two_lost_attempts_stop_at_policy_limit_without_third_container(
    history, tmp_path, image_id
):
    queue, repository, job = submit(history, tmp_path)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
            Worker(queue, fault=crash_after_execution).run_once()
        expire(queue, job)
    summary = Worker(queue).run_once()
    assert summary["status"] == "budget_exhausted", summary
    state, files, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 12 and state.attempts["code_exec"] == 2
    assert len(state.stopped_executions) == 2 and len(executions(history)) == 2
    assert "statistics.json" not in files


@pytest.mark.parametrize("point", ["sandbox_discard_transaction", "sandbox_after_discard"])
def test_discard_transaction_and_post_commit_crashes_never_double_charge(
    history, tmp_path, image_id, point
):
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    expire(queue, job)

    def crash(current):
        if current == point:
            raise RuntimeError("旧执行结算崩溃")

    with pytest.raises(RuntimeError, match="旧执行结算崩溃"):
        Worker(queue, fault=crash).run_once()
    expire(queue, job)
    summary = Worker(queue).run_once()
    assert summary["status"] == "completed", summary
    state, _, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 14 and len(state.stopped_executions) == 1
    assert len(executions(history)) == 3
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["events"])
                .where(TABLES["events"].c.type == "sandbox.execution_discarded")
            ).scalar_one()
            == 1
        )


def test_cleanup_failure_rechecks_with_backoff_without_spawning_runs_or_replaying(
    history, tmp_path, image_id, monkeypatch
):
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    expire(queue, job)
    reconcile = ExecutionService.reconcile
    monkeypatch.setattr(ExecutionService, "reconcile", staticmethod(lambda *args: 0))
    assert Worker(queue).run_once()["status"] == "outcome_unknown"
    unknown_run = queue.get(repository, UUID(job.job_id))["run_id"]
    assert len(executions(history)) == 1
    assert Worker(queue).run_once() is None
    make_cleanup_available(queue, job)
    assert Worker(queue).run_once()["status"] == "outcome_unknown"
    assert queue.get(repository, UUID(job.job_id))["run_id"] == unknown_run
    assert len(executions(history)) == 1
    monkeypatch.setattr(ExecutionService, "reconcile", staticmethod(reconcile))
    make_cleanup_available(queue, job)
    summary = Worker(queue).run_once()
    assert summary["status"] == "completed", summary
    state, _, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 14 and len(state.stopped_executions) == 1
    assert len(executions(history)) == 3


def test_second_attempt_without_durable_discard_is_rejected_before_intent(
    history, tmp_path, image_id
):
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    expire(queue, job)
    lease = queue.claim("invalid-retry")
    session, _, _ = Worker(queue)._attach(lease)
    assert session.state.pending_tool is not None
    session._execution_context()
    payload = encode(session.state.pending_tool.arguments)
    context = ManagedCallContext(
        repository,
        lease.run_id,
        uuid4(),
        CallOccurrence(
            logical_step_id="code_exec",
            call_index=0,
            attempt=2,
            provider="tool:code_exec/v1",
            signature_digest=session.tools.signature("code_exec"),
            request_digest=object_digest(ObjectType.BLOB, payload),
        ),
        session.head,
        payload,
        "a" * 64,
        16384,
    )
    with pytest.raises(AgentGitError) as rejected:
        session.execution_service.stage(context)
    assert rejected.value.code == ErrorCode.FORBIDDEN
    assert len(executions(history)) == 1


def test_discard_cannot_authorize_modified_workspace_input(history, tmp_path, image_id):
    queue, repository, job = submit(history, tmp_path)
    with pytest.raises(RuntimeError, match="执行结果事务崩溃"):
        Worker(queue, fault=crash_after_execution).run_once()
    expire(queue, job)

    def crash(point):
        if point == "sandbox_after_discard":
            raise RuntimeError("丢弃后崩溃")

    with pytest.raises(RuntimeError, match="丢弃后崩溃"):
        Worker(queue, fault=crash).run_once()
    expire(queue, job)
    lease = queue.claim("changed-input")
    session, _, _ = Worker(queue)._attach(lease)
    session.workspace.write("changed.txt", "不能替换原输入", "text/plain")
    with pytest.raises(AgentGitError) as rejected:
        session.tools.prepare("code_exec", session.state.pending_tool.arguments)
    assert rejected.value.code == ErrorCode.REPLAY_MISMATCH
    assert len(executions(history)) == 1


def test_actual_worker_process_death_cleans_live_container_then_uses_new_attempt(
    history, tmp_path, image_id, engine
):
    queue, repository, job = submit(history, tmp_path)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    killed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "helpers/job_worker.py"),
            schema,
            str(queue.research.data_root),
            str(history.objects.root),
            str(tmp_path / "actual-calls.txt"),
            "sandbox_after_execution",
            "1",
        ],
        capture_output=True,
        timeout=35,
        check=False,
    )
    assert killed.returncode == 73, killed.stderr.decode(errors="replace")
    old = executions(history)[0]
    try:
        assert engine.request("GET", f"/containers/{old['container_id']}/json")["State"]["Running"]
        expire(queue, job)
        summary = Worker(queue).run_once()
        assert summary["status"] == "completed", summary
        state, files, _ = final(queue, repository, job)
        assert state.usage.tool_calls == 14 and state.attempts["code_exec"] == 2
        assert "results/statistics.json" in files and len(executions(history)) == 3
        assert (
            next(row for row in executions(history) if row["call_id"] == old["call_id"])[
                "result_ref"
            ]
            is None
        )
    finally:
        container = SandboxContainer(
            engine,
            image_id,
            ExecutionIdentity(
                str(old["run_id"]), str(old["call_id"]), old["attempt"], old["fencing_token"]
            ),
            old["effective_timeout_ms"],
        )
        container.container_id = old["container_id"]
        assert container.cleanup()


def test_active_user_code_cancellation_is_applied_only_after_container_cleanup(
    history, tmp_path, image_id, engine, monkeypatch
):
    execution_started = Event()
    execute = SandboxContainer.execute

    def observe_execution(container, *args, **kwargs):
        execution_started.set()
        return execute(container, *args, **kwargs)

    monkeypatch.setattr(SandboxContainer, "execute", observe_execution)
    monkeypatch.setattr(
        mock,
        "_STATISTICS_CODE",
        "from pathlib import Path\nimport time\n"
        # 用户代码在写入部分文件后保持活动，取消必须等容器清理完成后才生效。
        # 休眠窗口大于 CI 慢环境下的探测上限，避免用户程序先自行结束。
        "Path('partial.txt').write_text('partial')\ntime.sleep(120)\n",
    )
    queue, repository, job = submit(history, tmp_path)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(Worker(queue).run_once)
        # 容器完成启动、就绪和输入装载后再探测，避免测试 exec 干扰初始化。
        assert execution_started.wait(timeout=60), (
            running.result(timeout=0) if running.done() else "Worker 尚未进入用户代码执行阶段"
        )
        ready = False
        # 慢速 CI runner 上容器启动和首次执行可能远超本地，放宽有界等待；
        # 断言仍要求实际观察到用户代码写入的部分文件后才请求取消。
        deadline = monotonic() + 60
        while monotonic() < deadline:
            rows = executions(history)
            if rows and rows[0]["container_id"]:
                try:
                    probe = engine.exec(
                        rows[0]["container_id"],
                        [
                            "/usr/local/bin/python",
                            "-I",
                            "-c",
                            "from pathlib import Path; "
                            "print(int(Path('/workspace/partial.txt').exists()))",
                        ],
                        user="10001:10003",
                        timeout=3,
                        output_limit=32,
                    )
                    if probe.stdout.strip() == b"1":
                        ready = True
                        break
                except EngineError as error:
                    assert error.status in (404, 409)
            sleep(0.05)
        assert ready, (
            running.result(timeout=0) if running.done() else "未观察到用户代码创建的部分文件"
        )
        run = queue.run(repository, UUID(job.run_id))
        requested = queue.request_control(
            repository, UUID(job.run_id), "cancel", expected_version=run["state_version"]
        )
        assert requested["control_status"] == "requested"
        summary = running.result(timeout=30)
    assert summary["status"] == "cancelled", summary
    state, files, _ = final(queue, repository, job)
    assert state.usage.tool_calls == 11 and not state.stopped_executions
    assert "partial.txt" not in files and "statistics.json" not in files
    assert queue.run(repository, UUID(job.run_id))["control_status"] == "applied"
    assert executions(history)[0]["cleanup_confirmed"]
