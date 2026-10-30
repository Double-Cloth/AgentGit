"""组控制的实际请求、私有暂停归属、在途结算和事务恢复。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, rows, start, state
from test_collaboration_execution import flow as flow
from test_merge_process_crash import kill_interpreter

from agentgit.core.collaboration_controls import CollaborationControlRequest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus
from agentgit.runtime.collaboration_run import CollaborationRoleRunner
from agentgit.services.collaboration_controls import CollaborationControlService
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "collaboration-operator"


def command(flow, action, revision, key=None):
    queue, _, _, repo, receipt, _ = flow
    return CollaborationControlService(queue).request(
        repo,
        UUID(receipt.collaboration_id),
        CollaborationControlRequest(action=action, expected_revision=revision),
        actor=ACTOR,
        key=key or f"{action}-{revision}",
    )


def view(flow):
    queue, _, _, repo, receipt, _ = flow
    return CollaborationControlService(queue).status(
        repo, UUID(receipt.collaboration_id), actor=ACTOR
    )


def test_cancel_prepared_is_causal_idempotent_and_has_no_calls(flow):
    queue, _, _, _, _, _ = flow
    accepted = command(flow, "cancel", 0)
    assert command(flow, "cancel", 0) == accepted
    assert view(flow)["status"] == "cancelled" and not view(flow)["all_terminal"]
    drain(queue)
    assert command(flow, "cancel", 0) == accepted
    assert view(flow)["all_terminal"] and view(flow)["quiescent"]
    assert len(rows(queue, "external_calls")) == 0
    assert len(rows(queue, "collaboration_messages")) == 0
    children = [e for e in rows(queue, "events") if e["type"] == "collaboration.parent_stopped"]
    assert len(children) == 3 and all(
        str(e["caused_by_run_id"]) == accepted.source_run_id
        and e["caused_by_seq"] == accepted.source_seq
        for e in children
    )


def test_pause_and_resume_before_first_claim_do_not_reset_independent_budgets(flow):
    queue, _, _, _, _, _ = flow
    start(flow)
    accepted = command(flow, "pause", 0)
    assert accepted.decision == "accepted" and view(flow)["quiescent"]
    assert Worker(queue).run_once() is None and not rows(queue, "external_calls")
    command(flow, "resume", 1)
    drain(queue)
    assert view(flow)["status"] == "completed" and view(flow)["revision"] == 2
    assert command(flow, "pause", 0) == accepted
    assert len(rows(queue, "external_calls")) == 20


@pytest.mark.parametrize(
    "point",
    [
        "call_reserved",
        "after_call_intent",
        "after_saved_result",
        "export_ready_saved",
        "exchange_committed",
    ],
)
def test_group_pause_at_durable_boundaries_settles_then_resumes_once(flow, point):
    queue, _, _, _, _, _ = flow
    start(flow)
    assert Worker(queue).run_once()["status"] == "paused"
    paused = []

    def pause(actual):
        if actual == "collaboration." + point and not paused:
            paused.append(command(flow, "pause", 0))
            assert not view(flow)["quiescent"]
            with pytest.raises(AgentGitError) as caught:
                command(flow, "resume", 1, "early-resume")
            assert caught.value.code == ErrorCode.VERSION_CONFLICT

    result = Worker(queue, fault=pause).run_once()
    assert paused and result["status"] == (
        "completed" if point == "exchange_committed" else "paused"
    )
    assert view(flow)["quiescent"] and Worker(queue).run_once() is None
    before = rows(queue, "external_calls")
    assert (
        len(before)
        == {
            "call_reserved": 1,
            "after_call_intent": 2,
            "after_saved_result": 2,
            "export_ready_saved": 9,
            "exchange_committed": 9,
        }[point]
    )
    command(flow, "resume", 1, "early-resume")
    drain(queue)
    assert view(flow)["status"] == "completed"
    assert len(rows(queue, "external_calls")) == 20
    assert (
        len(rows(queue, "collaboration_messages"))
        == len(rows(queue, "collaboration_receipts"))
        == 7
    )
    current, _ = state(flow, "researcher")
    assert current.usage.model_calls == 1 and current.usage.reserved_tokens == 0


def test_individual_human_pause_is_not_owned_or_cleared_by_group_resume(flow):
    queue, _, _, repo, _, roles = flow
    start(flow)
    Worker(queue).run_once()
    target = UUID(roles["researcher"]["run_id"])
    paused = []

    def pause(point):
        if point == "collaboration.after_saved_result" and not paused:
            queue.request_control(
                repo, target, "pause", expected_version=queue.run(repo, target)["state_version"]
            )
            paused.append(True)

    assert Worker(queue, fault=pause).run_once()["status"] == "paused"
    command(flow, "pause", 0)
    command(flow, "resume", 1)
    assert Worker(queue).run_once() is None
    held = queue.run(repo, target)
    assert held["control_request"] == "pause" and held["control_status"] == "applied"
    queue.request_control(repo, target, "resume", expected_version=held["state_version"])
    drain(queue)
    assert view(flow)["status"] == "completed" and len(rows(queue, "external_calls")) == 20


def test_cancel_supersedes_pending_group_pause_and_releases_unstarted_reservation(flow):
    queue, _, _, _, _, _ = flow
    start(flow)
    Worker(queue).run_once()
    cancelled = []

    def cancel(point):
        if point == "collaboration.call_reserved" and not cancelled:
            command(flow, "pause", 0)
            cancelled.append(command(flow, "cancel", 1))

    assert Worker(queue, fault=cancel).run_once()["status"] == "cancelled"
    drain(queue)
    current, _ = state(flow, "researcher")
    assert (
        current.pending is None
        and current.usage.reserved_tokens == current.usage.model_calls == current.usage.steps == 0
    )
    assert len(rows(queue, "external_calls")) == 1 and view(flow)["all_terminal"]
    assert any(e["type"] == "collaboration.call_aborted" for e in rows(queue, "events"))


def test_cancel_after_saved_result_preserves_observation_without_next_tool(flow):
    queue, _, _, _, _, _ = flow
    start(flow)
    Worker(queue).run_once()
    cancelled = []

    def cancel(point):
        if point == "collaboration.after_saved_result" and not cancelled:
            cancelled.append(command(flow, "cancel", 0))

    assert Worker(queue, fault=cancel).run_once()["status"] == "cancelled"
    drain(queue)
    current, _ = state(flow, "researcher")
    assert current.usage.model_calls == 1 and current.usage.tool_calls == 0
    assert current.pending is None and current.usage.reserved_tokens == 0
    assert len(rows(queue, "external_calls")) == 2


def test_owner_repository_revision_and_original_key_are_enforced_before_effects(flow):
    queue, _, _, repo, receipt, _ = flow
    controls = CollaborationControlService(queue)
    req = CollaborationControlRequest(action="cancel", expected_revision=0)
    for target, actor, code in [
        (repo, "another-operator", ErrorCode.FORBIDDEN),
        (uuid4(), ACTOR, ErrorCode.NOT_FOUND),
    ]:
        with pytest.raises(AgentGitError) as caught:
            controls.request(
                target, UUID(receipt.collaboration_id), req, actor=actor, key="unauthorized"
            )
        assert caught.value.code == code
    with pytest.raises(AgentGitError):
        command(flow, "cancel", 99)
    assert view(flow)["revision"] == 0 and not rows(queue, "events")
    start(flow)
    command(flow, "pause", 0, "one-key")
    for action, revision, key in [
        ("cancel", 0, "one-key"),
        ("resume", 0, "stale"),
        ("pause", 1, "new-pause"),
    ]:
        with pytest.raises(AgentGitError) as caught:
            command(flow, action, revision, key)
        assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert view(flow)["revision"] == 1 and not rows(queue, "external_calls")


def test_resume_keeps_elapsed_role_deadline_and_reports_budget_exhaustion(flow):
    queue, _, _, repo, _, roles = flow
    start(flow)
    Worker(queue).run_once()
    paused = []

    def pause(point):
        if point == "collaboration.after_saved_result" and not paused:
            paused.append(command(flow, "pause", 0))

    Worker(queue, fault=pause).run_once()
    target = UUID(roles["researcher"]["job_id"])
    with queue.history.engine.begin() as c:
        c.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == target)
            .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    before = queue.get(repo, target)
    command(flow, "resume", 1)
    drain(queue)
    assert view(flow)["status"] == "budget_exhausted" and len(rows(queue, "external_calls")) == 2
    assert queue.get(repo, target)["deadline_at"] == before["deadline_at"]
    assert queue.get(repo, target)["started_at"] == before["started_at"]


def test_constructor_failure_is_propagated_even_when_group_was_just_paused(flow):
    queue, _, coordinator, _, _, _ = flow
    start(flow)
    Worker(queue).run_once()

    def model(role):
        assert role == "researcher"
        command(flow, "pause", 0)
        raise AgentGitError(ErrorCode.FORBIDDEN, "测试构造期间停止")

    assert Worker(queue, collaboration_model_factory=model).run_once()["status"] == "failed"
    assert view(flow)["status"] == "paused"
    coordinator.tick()
    drain(queue)
    assert view(flow)["status"] == "failed" and len(rows(queue, "external_calls")) == 1


@pytest.mark.parametrize("point", ["projected", "committed"])
def test_fault_preserves_atomic_control_and_original_receipt(flow, point):
    queue, _, _, _, _, _ = flow

    def fail(actual):
        if actual == "collaboration.control_" + point:
            raise RuntimeError("测试组控制中断")

    queue.history.fault = fail
    with pytest.raises(RuntimeError, match="测试组控制中断"):
        command(flow, "cancel", 0)
    assert view(flow)["revision"] == int(point == "committed")
    queue.history.fault = lambda _: None
    result = command(flow, "cancel", 0)
    assert command(flow, "cancel", 0) == result and view(flow)["revision"] == 1
    drain(queue)
    assert not rows(queue, "external_calls")


@pytest.mark.parametrize("point", ["projected", "committed"])
def test_actual_control_process_death_rolls_back_or_returns_same_receipt(flow, tmp_path, point):
    queue, _, _, repo, receipt, _ = flow
    with queue.history.engine.connect() as c:
        schema = c.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "control-boundary.json"
    env = os.environ.copy()
    env.update(
        AGENTGIT_COLLAB_CONTROL_SCHEMA=schema,
        AGENTGIT_COLLAB_CONTROL_DATABASE=queue.history.engine.url.render_as_string(
            hide_password=False
        ),
        AGENTGIT_COLLAB_CONTROL_OBJECTS=str(queue.history.objects.root),
        AGENTGIT_COLLAB_CONTROL_DATA=str(queue.research.data_root),
        AGENTGIT_COLLAB_CONTROL_REPO=str(repo),
        AGENTGIT_COLLAB_CONTROL_GROUP=receipt.collaboration_id,
        AGENTGIT_COLLAB_CONTROL_POINT="collaboration.control_" + point,
        AGENTGIT_COLLAB_CONTROL_MARKER=str(marker),
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "helpers/collaboration_control_process.py"),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = monotonic() + 25
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode("utf-8", errors="replace"))
            assert monotonic() < deadline, "控制子进程未抵达确切边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text("utf-8"))
        assert boundary["parent_pid"] == process.pid or boundary["pid"] == process.pid
        assert (
            boundary["source"] == source_revision() and boundary["lock"] == dependency_lock_digest()
        )
        assert process.poll() is None
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.kill()
        process.wait(timeout=10)
        assert process.returncode == (73 if os.name == "nt" else -9)
        assert view(flow)["revision"] == int(point == "committed")
        accepted = command(flow, "cancel", 0, "process-control")
        assert command(flow, "cancel", 0, "process-control") == accepted
        drain(queue)
        assert not rows(queue, "external_calls") and view(flow)["all_terminal"]
        (tmp_path / "process-proof.json").write_text(
            json.dumps(
                dict(
                    boundary=boundary,
                    launcher_pid=process.pid,
                    exit_code=process.returncode,
                    receipt=accepted.model_dump(mode="json"),
                ),
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()


def test_group_cancel_keeps_unresolved_intent_unknown_after_actual_worker_death(flow, tmp_path):
    queue, _, _, repo, receipt, roles = flow
    start(flow)
    with queue.history.engine.connect() as c:
        schema = c.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker, counts = tmp_path / "intent-boundary.json", tmp_path / "actual-model-calls.jsonl"
    env = os.environ.copy()
    env.update(
        AGENTGIT_COLLAB_TEST_SCHEMA=schema,
        AGENTGIT_COLLAB_TEST_POINT="collaboration.after_call_intent",
        AGENTGIT_COLLAB_TEST_DATABASE=queue.history.engine.url.render_as_string(
            hide_password=False
        ),
        AGENTGIT_COLLAB_TEST_OBJECTS=str(queue.history.objects.root),
        AGENTGIT_COLLAB_TEST_DATA=str(queue.research.data_root),
        AGENTGIT_COLLAB_TEST_MARKER=str(marker),
        AGENTGIT_COLLAB_TEST_CALLS=str(counts),
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "helpers/collaboration_worker_process.py"),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = monotonic() + 25
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode("utf-8", errors="replace"))
            assert monotonic() < deadline, "实际 Worker 未抵达调用意图边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text("utf-8"))
        assert boundary["parent_pid"] == process.pid or boundary["pid"] == process.pid
        assert (
            boundary["source_revision"] == source_revision()
            and boundary["lock_digest"] == dependency_lock_digest()
        )
        accepted = command(flow, "cancel", 0)
        assert process.poll() is None
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.kill()
        process.wait(timeout=10)
        assert process.returncode == (73 if os.name == "nt" else -9)
        with queue.history.engine.begin() as c:
            c.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == UUID(roles["supervisor"]["job_id"]))
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        drain(queue)
        current, _ = state(flow, "supervisor")
        assert (
            current.status.value == "outcome_unknown"
            and current.pending is not None
            and current.usage.reserved_tokens > 0
        )
        assert (
            view(flow)["status"] == "cancelled"
            and view(flow)["has_unknown"]
            and view(flow)["all_terminal"]
        )
        assert (
            len(rows(queue, "external_calls")) == 1
            and rows(queue, "external_calls")[0]["result_ref"] is None
        )
        assert not counts.exists() or not counts.read_text("utf-8").strip()
        assert command(flow, "cancel", 0) == accepted and Worker(queue).run_once() is None
        (tmp_path / "process-proof.json").write_text(
            json.dumps(
                dict(
                    boundary=boundary,
                    launcher_pid=process.pid,
                    exit_code=process.returncode,
                    actual_models=0,
                    receipt=accepted.model_dump(mode="json"),
                ),
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()


@pytest.mark.parametrize("same_key", [True, False])
def test_parallel_requests_keep_one_revision_and_stable_original_receipt(flow, same_key):
    queue, _, _, _, _, _ = flow
    start(flow)

    def request(number):
        try:
            return command(flow, "pause", 0, "parallel" if same_key else "parallel-" + str(number))
        except AgentGitError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (1, 2)))
    if same_key:
        assert results[0] == results[1] and not isinstance(results[0], ErrorCode)
    else:
        assert sum(r == ErrorCode.VERSION_CONFLICT for r in results) == 1
    assert view(flow)["revision"] == 1 and view(flow)["quiescent"]
    assert (
        len(
            [
                e
                for e in rows(queue, "events")
                if e["type"] == "collaboration.group_control_requested"
            ]
        )
        == 1
    )
    assert not rows(queue, "external_calls")
    command(flow, "resume", 1)
    drain(queue)
    assert view(flow)["status"] == "completed" and len(rows(queue, "external_calls")) == 20


def test_late_cancel_is_not_marked_applied_by_an_already_prepared_pause_snapshot(flow, monkeypatch):
    queue, _, _, repo, _, _ = flow
    start(flow)
    Worker(queue).run_once()
    lease = queue.claim("late-group-cancel")
    paused = []

    def pause(point):
        if point == "collaboration.after_saved_result" and not paused:
            paused.append(command(flow, "pause", 0))

    runner = CollaborationRoleRunner(queue, lease, fault=pause)
    original = runner.exchange.save
    cancelled = []

    def save(current, event_type, **kwargs):
        if (
            event_type == "collaboration.stopped"
            and current.status == RunStatus.PAUSED
            and not cancelled
        ):
            cancelled.append(command(flow, "cancel", 1))
        return original(current, event_type, **kwargs)

    monkeypatch.setattr(runner.exchange, "save", save)
    assert runner.run()["status"] == "paused" and cancelled
    pending = queue.run(repo, lease.run_id)
    assert pending["control_request"] == "cancel" and pending["control_status"] == "requested"
    drain(queue)
    final = queue.run(repo, lease.run_id)
    assert final["status"] == "cancelled" and final["control_status"] == "applied"
    assert view(flow)["all_terminal"] and len(rows(queue, "external_calls")) == 2
