"""新 Recorded Run 的实际 Worker、来源账本与控制，不构造执行客户端。"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from test_collaboration_controls import command
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, rows, start
from test_collaboration_execution import flow as flow
from test_collaboration_recorded_preparation import completed as completed
from test_collaboration_recorded_preparation import prepare
from test_merge_process_crash import kill_interpreter

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType
from agentgit.core.collaboration_controls import CollaborationControlRequest
from agentgit.runtime import collaboration_recorded as recorded_module
from agentgit.services.collaboration_controls import CollaborationControlService
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "collaboration-operator"


def proof(request, **value):
    destination = os.environ.get("AGENTGIT_COLLABORATION_RECORDED_WORKER_PROOF_DIR")
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
        (root / name).write_text(
            json.dumps(dict(test=request.node.nodeid, **value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def finish(flow, monkeypatch, request):
    queue, service, coordinator, repo, source, _ = flow
    receipt = prepare(flow)
    before = queue.history.branch(repo)
    coordinator.start(repo, UUID(receipt.collaboration_id), actor=ACTOR, key="recorded-start")

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded Worker 不能构造执行客户端")

    with monkeypatch.context() as selected:
        for cls in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
            selected.setattr(cls, "__init__", forbidden)
        selected.setattr(subprocess, "Popen", forbidden)
        results = drain(queue)
    summary = service.summary(repo, UUID(receipt.collaboration_id))
    assert summary["status"] == "completed", results
    target = {r["run_id"] for r in summary["roles"]}
    calls = rows(queue, "external_calls")
    physical = {str(c["id"]): c for c in calls if c["origin_call_id"] is None}
    recorded = [c for c in calls if str(c["run_id"]) in target]
    assert len(physical) == len(recorded) == 20
    for c in recorded:
        original = physical[str(c["origin_call_id"])]
        run = queue.run(repo, c["run_id"])
        assert (
            original["run_id"] == run["origin_run_id"]
            and c["intent_ref"] == original["intent_ref"]
            and c["result_ref"] == original["result_ref"]
        )
    steps = [r for r in rows(queue, "collaboration_recorded_steps") if str(r["run_id"]) in target]
    assert len(steps) >= 72
    assert (
        len(
            [
                m
                for m in rows(queue, "collaboration_messages")
                if str(m["collaboration_id"]) == receipt.collaboration_id
            ]
        )
        == 7
    )
    assert (
        len(
            [
                p
                for p in rows(queue, "collaboration_publications")
                if str(p["collaboration_id"]) == receipt.collaboration_id
            ]
        )
        == 5
    )
    assert queue.history.branch(repo) == before and Worker(queue).run_once() is None
    proof(
        request,
        source_group=source.collaboration_id,
        target_group=receipt.collaboration_id,
        source_physical_calls=20,
        target_reused_calls=20,
        source_steps=len(steps),
        actual_target_calls=0,
    )


def test_new_recorded_runs_complete_with_own_source_ledgers_and_no_clients(
    completed, monkeypatch, request
):
    finish(completed, monkeypatch, request)
    queue = completed[0]
    table = TABLES["collaboration_recorded_steps"]
    first = rows(queue, "collaboration_recorded_steps")[0]
    for change in (
        table.update().values(source_index_ref=first["source_index_ref"]),
        table.delete(),
    ):
        with pytest.raises(sa.exc.DBAPIError), queue.history.engine.begin() as c:
            c.execute(change)
    source_runs = {r["origin_run_id"] for r in rows(queue, "runs") if r["mode"] == "recorded"}
    wrong = next(
        s
        for s in rows(queue, "snapshots")
        if s["run_id"] in source_runs and s["run_id"] != first["source_run_id"]
    )
    with (
        pytest.raises(sa.exc.DBAPIError, match="actual own source"),
        queue.history.engine.begin() as c,
    ):
        c.execute(
            table.insert().values(
                **{
                    **dict(first),
                    "source_run_id": wrong["run_id"],
                    "source_commit_id": wrong["commit_id"],
                }
            )
        )


@pytest.mark.parametrize(
    "point",
    [
        "collaboration.call_reserved",
        "collaboration.after_saved_result",
        "collaboration.export_ready_saved",
    ],
)
def test_historical_group_controls_remain_fixed_source_facts(flow, point, monkeypatch, request):
    queue = flow[0]
    start(flow)
    Worker(queue).run_once()
    paused = []

    def pause(current):
        if current == point and not paused:
            command(flow, "pause", 0)
            paused.append(True)

    assert Worker(queue, fault=pause).run_once()["status"] == "paused"
    command(flow, "resume", 1)
    drain(queue)
    finish(flow, monkeypatch, request)


def test_current_group_pause_resume_preserves_recorded_progress(completed, request):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    coordinator.start(repo, UUID(receipt.collaboration_id), actor=ACTOR, key="start")
    stopped = []

    def pause(point):
        if point == "collaboration.recorded_frame_saved" and not stopped:
            CollaborationControlService(queue).request(
                repo,
                UUID(receipt.collaboration_id),
                CollaborationControlRequest(action="pause", expected_revision=0),
                actor=ACTOR,
                key="pause",
            )
            stopped.append(True)

    assert Worker(queue, fault=pause).run_once()["status"] == "paused"
    progress = len(rows(queue, "collaboration_recorded_steps"))
    assert progress == 1
    CollaborationControlService(queue).request(
        repo,
        UUID(receipt.collaboration_id),
        CollaborationControlRequest(action="resume", expected_revision=1),
        actor=ACTOR,
        key="resume",
    )
    assert all(r["status"] in ("paused", "completed") for r in drain(queue))
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
    assert len(rows(queue, "collaboration_recorded_steps")) == 72
    proof(
        request,
        target_group=receipt.collaboration_id,
        current_pause_progress=progress,
        final_progress=72,
    )


@pytest.mark.parametrize(
    "point,minimum",
    [("collaboration.recorded.in_transaction", 0), ("collaboration.recorded_frame_saved", 3)],
)
def test_actual_recorded_interpreter_death_recovers_checkpoint_and_call_ledger(
    completed, point, minimum, tmp_path, request
):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    coordinator.start(repo, UUID(receipt.collaboration_id), actor=ACTOR, key="start")
    with queue.history.engine.connect() as c:
        schema = c.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "marker.json"
    calls = tmp_path / "calls.jsonl"
    env = os.environ.copy()
    env.update(
        AGENTGIT_RECORD_SCHEMA=schema,
        AGENTGIT_RECORD_POINT=point,
        AGENTGIT_RECORD_MIN_STEPS=str(minimum),
        AGENTGIT_RECORD_DATABASE=queue.history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_RECORD_OBJECTS=str(queue.history.objects.root),
        AGENTGIT_RECORD_DATA=str(queue.research.data_root),
        AGENTGIT_RECORD_MARKER=str(marker),
        AGENTGIT_RECORD_CALLS=str(calls),
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "helpers/collaboration_recorded_worker_process.py"),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = monotonic() + 40
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode("utf-8", errors="replace"))
            assert monotonic() < deadline, "Recorded Worker 未到达指定边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text(encoding="utf-8"))
        assert (
            boundary["source"] == source_revision() and boundary["lock"] == dependency_lock_digest()
        )
        assert boundary["pid"] == process.pid or (
            os.name == "nt" and boundary["parent_pid"] == process.pid
        )
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.kill()
        process.wait(timeout=10)
        assert process.returncode == (73 if os.name == "nt" else -9)
        assert not calls.exists()
        before = len(rows(queue, "collaboration_recorded_steps"))
        assert before == minimum
        if minimum:
            owned = [r for r in rows(queue, "external_calls") if r["origin_call_id"] is not None]
            assert len(owned) == 1
        with queue.history.engine.begin() as c:
            c.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.status == "running")
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        drain(queue)
        assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
        assert len(rows(queue, "collaboration_recorded_steps")) == 72
        assert (
            len([r for r in rows(queue, "external_calls") if r["origin_call_id"] is not None]) == 20
        )
        proof(
            request,
            target_group=receipt.collaboration_id,
            boundary=boundary,
            exit_code=process.returncode,
            launcher_pid=process.pid,
            before_progress=before,
            after_progress=72,
            forbidden_clients=0,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_missing_origin_response_after_prepare_fails_without_live_fallback(
    completed, monkeypatch, request
):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    call = rows(queue, "external_calls")[0]
    ref = queue.history.objects.get_json(call["result_ref"], ObjectType.CALL)["response_ref"]
    path = queue.history.objects.path_for(ref)
    raw = path.read_bytes()
    path.unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("缺录制不能回退 Live")

    try:
        coordinator.start(repo, UUID(receipt.collaboration_id), actor=ACTOR, key="start")
        with monkeypatch.context() as selected:
            for cls in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
                selected.setattr(cls, "__init__", forbidden)
            selected.setattr(subprocess, "Popen", forbidden)
            results = drain(queue)
        assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "failed", results
        assert (
            not rows(queue, "collaboration_recorded_steps")
            and len(rows(queue, "external_calls")) == 20
        )
        proof(
            request,
            target_group=receipt.collaboration_id,
            missing_response_ref=ref,
            failed_closed=True,
        )
    finally:
        path.write_bytes(raw)


def test_current_cancel_after_known_reuse_preserves_one_source_call(completed, request):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    coordinator.start(repo, UUID(receipt.collaboration_id), actor=ACTOR, key="start")
    cancelled = []

    def cancel(point):
        if (
            point == "collaboration.recorded_frame_saved"
            and not cancelled
            and len(rows(queue, "collaboration_recorded_steps")) == 3
        ):
            CollaborationControlService(queue).request(
                repo,
                UUID(receipt.collaboration_id),
                CollaborationControlRequest(action="cancel", expected_revision=0),
                actor=ACTOR,
                key="cancel",
            )
            cancelled.append(True)

    assert Worker(queue, fault=cancel).run_once()["status"] == "cancelled"
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "cancelled"
    assert len(rows(queue, "collaboration_recorded_steps")) == 3
    assert len([r for r in rows(queue, "external_calls") if r["origin_call_id"] is not None]) == 1
    proof(request, target_group=receipt.collaboration_id, cancelled_progress=3, reused_calls=1)


@pytest.mark.parametrize("at_steps", [0, 3])
def test_individual_cancel_stops_group_with_its_actual_causal_event(completed, at_steps, request):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    group_id = UUID(receipt.collaboration_id)
    supervisor = UUID(
        next(
            r["run_id"]
            for r in service.summary(repo, group_id)["roles"]
            if r["role"] == "supervisor"
        )
    )
    coordinator.start(repo, group_id, actor=ACTOR, key="start")
    requested = []

    def cancel(point):
        if not requested and (
            at_steps == 0
            or (
                point == "collaboration.recorded_frame_saved"
                and len(rows(queue, "collaboration_recorded_steps")) == at_steps
            )
        ):
            queue.request_control(
                repo,
                supervisor,
                "cancel",
                expected_version=queue.run(repo, supervisor)["state_version"],
            )
            requested.append(True)

    if at_steps == 0:
        cancel("before_claim")
    assert Worker(queue, fault=cancel).run_once()["status"] == "cancelled"
    drain(queue)
    group = next(g for g in rows(queue, "collaborations") if g["id"] == group_id)
    assert group["status"] == "cancelled" and group["failure_run_id"] == supervisor
    assert len(rows(queue, "collaboration_recorded_steps")) == at_steps
    assert len(
        [c for c in rows(queue, "external_calls") if c["origin_call_id"] is not None]
    ) == int(at_steps != 0)
    for event in rows(queue, "events"):
        if event["type"] == "collaboration.parent_stopped":
            assert (event["caused_by_run_id"], event["caused_by_seq"]) == (
                supervisor,
                group["failure_event_seq"],
            )
    proof(
        request,
        target_group=receipt.collaboration_id,
        individual_cancel_progress=at_steps,
        reused_calls=int(at_steps != 0),
    )


def test_current_wall_deadline_stops_as_budget_exhausted_without_reset(completed, request):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    group_id = UUID(receipt.collaboration_id)
    supervisor = next(
        r for r in service.summary(repo, group_id)["roles"] if r["role"] == "supervisor"
    )
    coordinator.start(repo, group_id, actor=ACTOR, key="start")
    deadlines = []

    def expire(point):
        if (
            point == "collaboration.recorded_frame_saved"
            and not deadlines
            and len(rows(queue, "collaboration_recorded_steps")) == 3
        ):
            with queue.history.engine.begin() as c:
                deadline = c.execute(
                    sa.select(sa.func.clock_timestamp() - sa.text("interval '1 second'"))
                ).scalar_one()
                c.execute(
                    TABLES["jobs"]
                    .update()
                    .where(TABLES["jobs"].c.id == UUID(supervisor["job_id"]))
                    .values(deadline_at=deadline)
                )
                deadlines.append(deadline)

    assert Worker(queue, fault=expire).run_once()["status"] == "budget_exhausted"
    drain(queue)
    assert service.summary(repo, group_id)["status"] == "budget_exhausted"
    assert queue.get(repo, UUID(supervisor["job_id"]))["deadline_at"] == deadlines[0]
    assert len(rows(queue, "collaboration_recorded_steps")) == 3
    assert len([c for c in rows(queue, "external_calls") if c["origin_call_id"] is not None]) == 1
    proof(
        request,
        target_group=receipt.collaboration_id,
        budget_progress=3,
        reused_calls=1,
        unchanged_deadline=deadlines[0].isoformat(),
    )


@pytest.mark.parametrize("expires_on_resume", [False, True])
def test_individual_pause_with_pending_recording_resumes_without_new_intent(
    completed, expires_on_resume, monkeypatch, request
):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare(completed)
    group_id = UUID(receipt.collaboration_id)
    target = UUID(
        next(
            r["run_id"]
            for r in service.summary(repo, group_id)["roles"]
            if r["role"] == "supervisor"
        )
    )
    coordinator.start(repo, group_id, actor=ACTOR, key="start")
    paused = []

    def pause(point):
        if (
            point == "collaboration.recorded_frame_saved"
            and not paused
            and len(rows(queue, "collaboration_recorded_steps")) == 2
        ):
            queue.request_control(
                repo, target, "pause", expected_version=queue.run(repo, target)["state_version"]
            )
            paused.append(True)

    assert Worker(queue, fault=pause).run_once()["status"] == "paused"
    held = queue.run(repo, target)
    state = queue.history.restore(repo, held["checkpoint_commit_id"]).data
    assert state["pending"] is not None and state["usage"]["reserved_tokens"] > 0
    assert not [c for c in rows(queue, "external_calls") if c["origin_call_id"] is not None]
    audit_attempts = []
    if expires_on_resume:
        original = recorded_module.verified_role_lane

        def bounded_audit(queue, lease):
            if lease.run_id == target:
                audit_attempts.append(True)
                assert len(audit_attempts) <= 2, "已到期恢复不能无限重试来源核验"
            return original(queue, lease)

        monkeypatch.setattr(recorded_module, "verified_role_lane", bounded_audit)
        with queue.history.engine.begin() as c:
            c.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.run_id == target)
                .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
    queue.request_control(repo, target, "resume", expected_version=held["state_version"])
    drain(queue)
    assert service.summary(repo, group_id)["status"] == (
        "budget_exhausted" if expires_on_resume else "completed"
    )
    progress, calls = (2, 0) if expires_on_resume else (72, 20)
    assert len(rows(queue, "collaboration_recorded_steps")) == progress
    assert (
        len([c for c in rows(queue, "external_calls") if c["origin_call_id"] is not None]) == calls
    )
    if expires_on_resume:
        assert len(audit_attempts) == 1
    proof(
        request,
        target_group=receipt.collaboration_id,
        individual_pause_progress=2,
        final_progress=progress,
        reused_calls=calls,
        expires_on_resume=expires_on_resume,
        audit_attempts=len(audit_attempts),
    )
