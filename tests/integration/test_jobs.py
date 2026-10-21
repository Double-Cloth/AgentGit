"""S4 持久化调度的真实 PostgreSQL 行为测试。"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import AgentState, ResearchGoal
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def queued(history, tmp_path):
    research = ResearchService(history, tmp_path / "jobs-data")
    repo = history.create_repository("持久化调度")
    config = research.create_config(repo)
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    options = dict(
        actor="local-operator",
        key="submit-1",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    return queue, repo, config, task, options


def submit(queued):
    queue, repo, _, task, options = queued
    return queue.enqueue(repo, task, **options)


def expire(queue, job_id):
    with queue.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(str(job_id)))
            .values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
        )


def test_enqueued_job_survives_service_restart_and_completes(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    assert not (queue.research.data_root / "workspaces").exists()
    restarted = JobService(ResearchService(queue.history, queue.research.data_root))
    result = Worker(restarted).run_once()
    assert result["status"] == "completed", result
    run = restarted.run(repo, UUID(response.run_id))
    assert run["status"] == "completed"
    assert (
        restarted.get(repo, UUID(response.job_id))["checkpoint_commit_id"]
        == run["checkpoint_commit_id"]
    )
    assert queue.history.branch(repo)["active_writer_run_id"] is None
    state = parse_contract(
        encode(queue.history.restore(repo, run["checkpoint_commit_id"]).data), AgentState
    )
    assert state.status == RunStatus.COMPLETED
    assert state.usage.model_calls == 11


def test_submission_is_atomic_and_idempotent_across_connections(queued):
    queue, repo, _, _, options = queued
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: submit(queued), range(2)))
    assert results[0] == results[1]
    with queue.history.engine.connect() as connection:
        for name in ("jobs", "runs", "idempotency_keys"):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                == 1
            )
    with pytest.raises(AgentGitError) as caught:
        queue.enqueue(repo, queued[3], **{**options, "expected_branch_version": 1})
    assert caught.value.code == ErrorCode.VERSION_CONFLICT


def test_branch_conflict_is_rejected_before_any_call(queued):
    queue, repo, config, _, options = queued
    response = submit(queued)
    other = queue.research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    with pytest.raises(AgentGitError) as caught:
        queue.enqueue(repo, other, **{**options, "key": "different-task"})
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert queue.history.branch(repo)["active_writer_run_id"] == UUID(response.run_id)
    with queue.history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["external_calls"])
            ).scalar_one()
            == 0
        )


def test_exactly_one_worker_claims_a_job(queued):
    queue, _, _, _, _ = queued
    submit(queued)
    with ThreadPoolExecutor(2) as pool:
        leases = list(pool.map(queue.claim, ("A", "B")))
    assert sum(lease is not None for lease in leases) == 1


def test_expired_worker_cannot_commit_before_or_after_takeover(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    first = queue.claim("A")
    session, _, _ = Worker(queue)._attach(first)
    expire(queue, response.job_id)
    second = queue.claim("B")
    assert second.fencing_token == first.fencing_token + 1
    assert second.recovery_required
    with pytest.raises(AgentGitError) as caught:
        session.execute()
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    with pytest.raises(AgentGitError):
        queue.heartbeat(first)
    assert queue.history.branch(repo)["head_commit_id"] is None


def test_unfenced_service_cannot_execute_managed_run(queued):
    queue, repo, _, _, _ = queued
    submit(queued)
    lease = queue.claim("A")
    session, _, _ = Worker(queue)._attach(lease)
    session.history = queue.history
    with pytest.raises(AgentGitError) as caught:
        session.execute()
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert queue.history.branch(repo)["head_commit_id"] is None


def test_queued_pause_and_resume_use_same_run_and_deadline(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    run_id = UUID(response.run_id)
    requested = queue.request_control(repo, run_id, "pause", expected_version=0)
    assert requested["status"] == "queued"
    assert requested["control_status"] == "requested"
    assert Worker(queue).run_once()["status"] == "paused"
    parked = queue.get(repo, UUID(response.job_id))
    assert queue.history.branch(repo)["active_writer_run_id"] == run_id
    current = queue.run(repo, run_id)
    queue.request_control(repo, run_id, "resume", expected_version=current["state_version"])
    assert Worker(queue).run_once()["status"] == "completed"
    done = queue.get(repo, UUID(response.job_id))
    assert done["run_id"] == run_id
    assert done["deadline_at"] == parked["deadline_at"]
    assert done["fencing_token"] == 2


def test_cancel_queued_run_never_calls_model(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    queue.request_control(repo, UUID(response.run_id), "cancel", expected_version=0)

    class NoModel(MockResearchModel):
        def invoke(self, payload):
            raise AssertionError("取消后不应调用模型")

    assert Worker(queue, model_factory=NoModel).run_once()["status"] == "cancelled"
    assert queue.history.branch(repo)["active_writer_run_id"] is None


def test_pause_during_call_stays_requested_until_safe_checkpoint(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    started, release = Event(), Event()

    class WaitingModel(MockResearchModel):
        def invoke(self, payload):
            started.set()
            assert release.wait(10)
            return super().invoke(payload)

    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(Worker(queue, model_factory=WaitingModel).run_once)
        assert started.wait(10)
        current = queue.run(repo, UUID(response.run_id))
        requested = queue.request_control(
            repo, UUID(response.run_id), "pause", expected_version=current["state_version"]
        )
        assert requested["status"] == "planning"
        assert requested["control_status"] == "requested"
        release.set()
        assert result.result(timeout=15)["status"] == "paused"
    current = queue.run(repo, UUID(response.run_id))
    assert current["status"] == "paused"
    assert current["control_status"] == "applied"


def test_expired_wall_deadline_is_not_reset_by_resume(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    run_id = UUID(response.run_id)
    queue.request_control(repo, run_id, "pause", expected_version=0)
    assert Worker(queue).run_once()["status"] == "paused"
    with queue.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(response.job_id))
            .values(deadline_at=sa.func.now() - sa.text("interval '1 second'"))
        )
    current = queue.run(repo, run_id)
    queue.request_control(repo, run_id, "resume", expected_version=current["state_version"])
    assert Worker(queue).run_once()["status"] == "budget_exhausted"


@pytest.mark.parametrize(
    "point,number", [("after_checkpoint", 4), ("after_result", 1), ("after_result", 17)]
)
def test_real_process_crash_reuses_saved_results_in_new_run(queued, tmp_path, point, number):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    original = UUID(response.run_id)
    with queue.history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    helper = Path(__file__).resolve().parents[1] / "helpers/job_worker.py"
    marker = tmp_path / "actual-calls.txt"

    def launch(failure, occurrence):
        return subprocess.run(
            [
                sys.executable,
                str(helper),
                schema,
                str(queue.research.data_root),
                str(queue.history.objects.root),
                str(marker),
                failure,
                str(occurrence),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )

    killed = launch(point, number)
    assert killed.returncode == 73, killed.stderr.decode(errors="replace")
    old_checkpoint = queue.run(repo, original)["checkpoint_commit_id"]
    old_content = queue.history.restore(repo, old_checkpoint).data
    expire(queue, response.job_id)
    restarted = launch("none", 0)
    assert restarted.returncode == 0, restarted.stderr.decode(errors="replace")
    job = queue.get(repo, UUID(response.job_id))
    assert job["status"] == "completed", restarted.stdout.decode(errors="replace")
    assert job["run_id"] != original
    assert job["fencing_token"] == 2
    old = queue.run(repo, original)
    assert old["status"] == "interrupted"
    assert old["checkpoint_commit_id"] == old_checkpoint
    assert queue.history.restore(repo, old_checkpoint).data == old_content
    assert queue.run(repo, job["run_id"])["origin_run_id"] == original
    actual = marker.read_text(encoding="utf-8").splitlines()
    assert actual.count("model") == 11
    assert actual.count("tool:corpus_search") == 1
    assert actual.count("tool:source_read") == 6
    assert actual.count("tool:artifact_write") == 2
    snapshot = queue.history.restore(repo, job["checkpoint_commit_id"])
    assert {artifact.path for artifact in snapshot.artifacts} == {"report.md", "sources.json"}


def test_crash_during_model_preserves_unknown_budget_and_does_not_resend(queued, tmp_path):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    with queue.history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "unknown-calls.txt"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "helpers/job_worker.py"),
            schema,
            str(queue.research.data_root),
            str(queue.history.objects.root),
            str(marker),
            "during_model",
            "1",
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    expire(queue, response.job_id)

    class NoModel(MockResearchModel):
        def invoke(self, payload):
            raise AssertionError("未决调用不能自动重发")

    recovered = Worker(queue, model_factory=NoModel).run_once()
    assert recovered["status"] == "outcome_unknown", recovered
    job = queue.get(repo, UUID(response.job_id))
    snapshot = queue.history.restore(repo, job["checkpoint_commit_id"])
    state = parse_contract(encode(snapshot.data), AgentState)
    assert state.status == RunStatus.OUTCOME_UNKNOWN
    assert state.usage.steps == state.usage.model_calls == 1
    assert state.usage.reserved_tokens > 0
    assert marker.read_text(encoding="utf-8").splitlines() == ["model"]
    current = queue.run(repo, job["run_id"])
    queue.request_control(repo, job["run_id"], "cancel", expected_version=current["state_version"])
    assert Worker(queue, model_factory=NoModel).run_once() is None
    assert queue.run(repo, job["run_id"])["control_status"] == "requested"
    assert queue.get(repo, job["id"])["status"] == "outcome_unknown"


def test_heartbeat_renews_lease_while_adapter_is_waiting(queued):
    queue, repo, _, _, _ = queued
    queue.lease_ms = 900
    response = submit(queued)
    started, release = Event(), Event()

    class WaitingModel(MockResearchModel):
        def invoke(self, payload):
            started.set()
            assert release.wait(10)
            return super().invoke(payload)

    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(Worker(queue, model_factory=WaitingModel).run_once)
        assert started.wait(10)
        before = queue.get(repo, UUID(response.job_id))["lease_until"]
        # 等待跨过最初租约，真实心跳必须使其继续可用。
        target = monotonic() + 1.2
        while monotonic() < target:
            assert not release.wait(0.1)
        try:
            assert queue.get(repo, UUID(response.job_id))["lease_until"] > before
            assert queue.claim("competitor") is None
        finally:
            release.set()
        assert result.result(timeout=20)["status"] == "completed"


def test_claim_skips_locked_job_and_can_claim_after_release(queued):
    queue, _, _, _, _ = queued
    response = submit(queued)
    with queue.history.engine.begin() as connection:
        connection.execute(
            sa.select(TABLES["jobs"].c.id)
            .where(TABLES["jobs"].c.id == UUID(response.job_id))
            .with_for_update()
        ).scalar_one()
        assert queue.claim("locked-competitor") is None
    lease = queue.claim("after-release")
    assert lease is not None and lease.job_id == UUID(response.job_id)


def test_late_adapter_response_cannot_commit_after_takeover(queued):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    started, release = Event(), Event()

    class WaitingModel(MockResearchModel):
        def invoke(self, payload):
            started.set()
            assert release.wait(10)
            return super().invoke(payload)

    with ThreadPoolExecutor(1) as pool:
        late = pool.submit(Worker(queue, model_factory=WaitingModel).run_once)
        assert started.wait(10)
        expire(queue, response.job_id)
        try:
            takeover = Worker(queue).run_once()
            assert takeover["status"] == "outcome_unknown", takeover
            checkpoint = queue.history.branch(repo)["head_commit_id"]
        finally:
            release.set()
        assert late.result(timeout=15)["status"] == "lease_lost"
    assert queue.history.branch(repo)["head_commit_id"] == checkpoint
    with queue.history.engine.connect() as connection:
        row = connection.execute(sa.select(TABLES["external_calls"])).mappings().one()
        assert row["result_ref"] is None


def test_terminal_retry_creates_fresh_run_and_does_not_reuse_origin_calls(queued):
    queue, repo, _, task, options = queued
    original = submit(queued)
    assert Worker(queue).run_once()["status"] == "completed"
    assert submit(queued) == original
    branch = queue.history.branch(repo)
    retry = queue.enqueue(
        repo,
        task,
        **{
            **options,
            "key": "fresh-retry",
            "expected_head": branch["head_commit_id"],
            "expected_branch_version": branch["version"],
        },
    )
    assert retry.run_id != original.run_id
    actual = []

    class CountingModel(MockResearchModel):
        def invoke(self, payload):
            actual.append(payload)
            return super().invoke(payload)

    done = Worker(queue, model_factory=CountingModel).run_once()
    assert done["status"] == "completed", done
    assert len(actual) == 11
    job = queue.get(repo, UUID(retry.job_id))
    assert job["origin_run_id"] is None
    assert queue.run(repo, UUID(retry.run_id))["origin_run_id"] == UUID(original.run_id)
    state = queue.history.restore(repo, job["checkpoint_commit_id"]).data
    assert state["plan_version"] == 2


def test_enqueue_failure_rolls_back_job_run_binding_and_idempotency(queued, monkeypatch):
    queue, repo, _, task, _ = queued
    with queue.history.engine.connect() as connection:
        initial_refs = connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["object_refs"])
        ).scalar_one()
    original_ref = queue.history._ref
    count = 0

    def fail_second_ref(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("注入响应引用写入失败")
        return original_ref(*args, **kwargs)

    monkeypatch.setattr(queue.history, "_ref", fail_second_ref)
    with pytest.raises(RuntimeError, match="注入"):
        submit(queued)
    assert queue.history.branch(repo)["active_writer_run_id"] is None
    with queue.history.engine.connect() as connection:
        for name in ("jobs", "runs", "idempotency_keys", "object_refs"):
            assert connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one() == (initial_refs if name == "object_refs" else 0)
        assert (
            connection.execute(
                sa.select(TABLES["tasks"].c.active_run_id).where(TABLES["tasks"].c.id == task)
            ).scalar_one()
            is None
        )


def test_job_and_run_queries_enforce_repository_scope(queued):
    queue, _, _, _, _ = queued
    response = submit(queued)
    other = queue.history.create_repository("其他项目")
    for getter, entity_id in (
        (queue.get, response.job_id),
        (queue.run, response.run_id),
    ):
        with pytest.raises(AgentGitError) as caught:
            getter(other, UUID(entity_id))
        assert caught.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    "target,action", [("model", "pause"), ("tool", "pause"), ("tool", "cancel")]
)
def test_control_between_runtime_check_and_intent_stops_new_call(
    queued, monkeypatch, target, action
):
    queue, repo, _, _, _ = queued
    response = submit(queued)
    run_id = UUID(response.run_id)
    original = RecordedCallGateway.call
    requested = False

    def interrupt(gateway, **kwargs):
        nonlocal requested
        matches = (
            kwargs["provider"].startswith("llm:")
            if target == "model"
            else kwargs["provider"].startswith("tool:")
        )
        if matches and not requested:
            requested = True
            current = queue.run(repo, run_id)
            queue.request_control(repo, run_id, action, expected_version=current["state_version"])
        return original(gateway, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(RecordedCallGateway, "call", interrupt)
        assert Worker(queue).run_once()["status"] == (
            "paused" if action == "pause" else "cancelled"
        )
    current = queue.run(repo, run_id)
    assert current["control_status"] == "applied"
    state = parse_contract(
        encode(queue.history.restore(repo, current["checkpoint_commit_id"]).data), AgentState
    )
    assert state.usage.reserved_tokens == 0
    assert state.usage.tool_calls == 0
    assert state.usage.model_calls == (0 if target == "model" else 2)
    with queue.history.engine.connect() as connection:
        calls = list(connection.execute(sa.select(TABLES["external_calls"])).mappings())
    assert len(calls) == state.usage.model_calls
    if action == "pause":
        queue.request_control(repo, run_id, "resume", expected_version=current["state_version"])
        done = Worker(queue).run_once()
        assert done["run_id"] == response.run_id
        assert done["status"] == "completed", done
