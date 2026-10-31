"""真实组窗口、多 Worker 和组／角色控制交错；不直接改写调度状态。"""

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, get_native_id
from time import monotonic
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from test_collaboration_execution import PROGRAM, drain, rows
from test_collaboration_execution import archive as archive
from test_collaboration_recorded_preparation import completed as completed
from test_collaboration_recorded_preparation import prepare as prepare_recorded

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.collaboration import CollaborationProgram, CollaborationRole
from agentgit.core.collaboration_controls import CollaborationControlRequest
from agentgit.core.collaboration_execution import RoleModelRequest
from agentgit.core.collaboration_runs import RolePrivateInput
from agentgit.core.models import parse_contract
from agentgit.services.collaboration import CollaborationService
from agentgit.services.collaboration_control import MAX_GROUPS_PER_TICK, CollaborationCoordinator
from agentgit.services.collaboration_controls import CollaborationControlService
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "collaboration-operator"


def proof(request, **value):
    directory = os.environ.get("AGENTGIT_COLLABORATION_SCALE_PROOF_DIR")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        target = root / (hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json")
        with target.open("x", encoding="utf-8") as stream:
            json.dump(dict(test=request.node.nodeid, **value), stream, ensure_ascii=False, indent=2)


def prepare(queue, repositories, count):
    service = CollaborationService(queue)
    program = parse_contract(PROGRAM.read_bytes(), CollaborationProgram)
    groups = []
    for index in range(count):
        repo = repositories[index % len(repositories)]
        receipt = service.prepare(
            repo,
            program,
            actor=ACTOR,
            key=f"scale-prepare-{index}",
            private_inputs=tuple(
                RolePrivateInput(role=role, notes=(f"private-group-{index}-{role.value}",))
                for role in CollaborationRole
            ),
        )
        groups.append((repo, UUID(receipt.collaboration_id)))
    return groups


def parallel_drain(queue, *, limit):
    ready = Barrier(4)

    def work(index):
        worker = Worker(queue, name=f"scale-worker-{index}")
        identity = dict(pid=os.getpid(), thread_id=get_native_id(), worker=worker.name)
        ready.wait(timeout=15)
        started = monotonic()
        results = []
        for _ in range(limit):
            result = worker.run_once()
            if result is None:
                return results, dict(
                    **identity, started=started, finished=monotonic(), claims=len(results)
                )
            assert result["status"] in ("paused", "completed", "cancelled"), result
            results.append(result)
        raise AssertionError("并发调度超过事先限定的领取次数")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(work, index) for index in range(4)]
        finished = [future.result(timeout=180) for future in futures]
    workers = [identity for _, identity in finished]
    assert len({worker["thread_id"] for worker in workers}) == 4
    assert all(worker["claims"] > 0 for worker in workers)
    assert max(worker["started"] for worker in workers) < min(
        worker["finished"] for worker in workers
    )
    return [result for results, _ in finished for result in results], workers


def test_real_hundred_group_window_skips_locks_and_drains_without_calls(history, tmp_path, request):
    assert MAX_GROUPS_PER_TICK == 100
    queue = JobService(ResearchService(history, tmp_path / "scale"))
    coordinator = CollaborationCoordinator(queue)
    repositories = [history.create_repository(f"百组窗口项目 {i}") for i in range(3)]
    started = monotonic()
    groups = prepare(queue, repositories, 105)
    tasks, jobs = TABLES["collaboration_tasks"], TABLES["jobs"]
    with history.engine.connect() as c:
        supervisors = (
            c.execute(sa.select(tasks.c.job_id).where(tasks.c.role == "supervisor")).scalars().all()
        )
    # NO KEY UPDATE 不阻碍开始收据的 FK 校验，但会让真实唤醒的 SKIP LOCKED 跳过 Job。
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with history.engine.begin() as held:
            held.execute(
                sa.select(jobs.c.id)
                .where(jobs.c.id.in_(supervisors))
                .with_for_update(key_share=True)
            ).all()
            for index, (repo, group) in enumerate(groups):
                pool.submit(
                    coordinator.start, repo, group, actor=ACTOR, key=f"scale-start-{index}"
                ).result(timeout=15)
            assert all(job["status"] == "paused" for job in rows(queue, "jobs"))
    finally:
        pool.shutdown(wait=True)
    queued = []
    # 最早组暂时被另一个事务持有，不能堵住后面的真实 100 组窗口。
    with history.engine.begin() as held:
        held.execute(
            sa.select(TABLES["collaborations"].c.id)
            .where(TABLES["collaborations"].c.id == groups[0][1])
            .with_for_update()
        ).one()
        for expected in (100, 104):
            coordinator.tick()
            queued.append(sum(job["status"] == "queued" for job in rows(queue, "jobs")))
            assert queued[-1] == expected
    coordinator.tick()
    queued.append(sum(job["status"] == "queued" for job in rows(queue, "jobs")))
    assert queued == [100, 104, 105] and not rows(queue, "external_calls")
    controls = CollaborationControlService(queue)
    for index, (repo, group) in enumerate(groups):
        controls.request(
            repo,
            group,
            CollaborationControlRequest(action="cancel", expected_revision=0),
            actor=ACTOR,
            key=f"scale-cancel-{index}",
        )
    results, workers = parallel_drain(queue, limit=500)
    assert len(results) == 420 and all(result["status"] == "cancelled" for result in results)
    assert all(run["status"] == "cancelled" for run in rows(queue, "runs"))
    assert all(
        job["status"] == "cancelled" and job["worker"] is None for job in rows(queue, "jobs")
    )
    assert not rows(queue, "external_calls") and not rows(queue, "collaboration_messages")
    assert Worker(queue).run_once() is None
    proof(
        request,
        groups=105,
        repositories=3,
        workers=4,
        queued_after_ticks=queued,
        claims=len(results),
        physical_calls=0,
        worker_observations=workers,
        results=results,
        elapsed_seconds=monotonic() - started,
    )


def test_four_workers_complete_twelve_groups_without_cross_role_private_context(
    history, tmp_path, request
):
    queue = JobService(ResearchService(history, tmp_path / "parallel"))
    coordinator = CollaborationCoordinator(queue)
    repositories = [history.create_repository(f"并发研究项目 {i}") for i in range(3)]
    groups = prepare(queue, repositories, 12)
    mains = {repo: history.branch(repo) for repo in repositories}
    started = monotonic()
    for index, (repo, group) in enumerate(groups):
        coordinator.start(repo, group, actor=ACTOR, key=f"parallel-start-{index}")
    results, workers = parallel_drain(queue, limit=120)
    assert all(group["status"] == "completed" for group in rows(queue, "collaborations"))
    assert all(run["status"] == "completed" for run in rows(queue, "runs"))
    assert {repo: history.branch(repo) for repo in repositories} == mains
    calls, tasks = rows(queue, "external_calls"), rows(queue, "collaboration_tasks")
    assert len(calls) == 240 and all(
        c["status"] == "succeeded" and c["origin_call_id"] is None for c in calls
    )
    assert (
        len(rows(queue, "collaboration_messages"))
        == len(rows(queue, "collaboration_receipts"))
        == 84
    )
    assert len(rows(queue, "collaboration_publications")) == 60
    by_run = {task["run_id"]: task for task in tasks}
    group_indices = {group: index for index, (_, group) in enumerate(groups)}
    for call in calls:
        if not call["provider"].startswith("llm:"):
            continue
        intent = history.objects.get_json(call["intent_ref"], ObjectType.CALL)
        value = parse_contract(
            history.objects.get(intent["request_ref"], ObjectType.BLOB), RoleModelRequest
        )
        task = by_run[call["run_id"]]
        expected = f"private-group-{group_indices[task['collaboration_id']]}-{task['role']}"
        assert value.private_notes == (expected,) and value.role.value == task["role"]
        assert encode(value.model_dump(mode="json")).count(b"private-group-") == 1
    assert Worker(queue).run_once() is None
    proof(
        request,
        groups=12,
        repositories=3,
        workers=4,
        claims=len(results),
        physical_calls=240,
        worker_observations=workers,
        results=results,
        messages=84,
        publications=60,
        elapsed_seconds=monotonic() - started,
    )


@pytest.mark.parametrize("ending", ["resume", "cancel"])
def test_recorded_individual_pending_pause_survives_group_cycle(
    completed, ending, monkeypatch, request
):
    queue, service, coordinator, repo, _, _ = completed
    receipt = prepare_recorded(completed)
    group = UUID(receipt.collaboration_id)
    supervisor = UUID(
        next(
            role["run_id"]
            for role in service.summary(repo, group)["roles"]
            if role["role"] == "supervisor"
        )
    )
    coordinator.start(repo, group, actor=ACTOR, key="recorded-start")
    paused = []

    def pause(point):
        if (
            point == "collaboration.recorded_frame_saved"
            and not paused
            and len(rows(queue, "collaboration_recorded_steps")) == 2
        ):
            queue.request_control(
                repo,
                supervisor,
                "pause",
                expected_version=queue.run(repo, supervisor)["state_version"],
            )
            paused.append(True)

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded 不能构造执行客户端")

    with monkeypatch.context() as selected:
        for cls in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
            selected.setattr(cls, "__init__", forbidden)
        selected.setattr(subprocess, "Popen", forbidden)
        assert Worker(queue, fault=pause).run_once()["status"] == "paused" and paused
        controls = CollaborationControlService(queue)
        controls.request(
            repo,
            group,
            CollaborationControlRequest(action="pause", expected_revision=0),
            actor=ACTOR,
            key="pause-group",
        )
        controls.request(
            repo,
            group,
            CollaborationControlRequest(action="resume", expected_revision=1),
            actor=ACTOR,
            key="resume-group",
        )
        held = queue.run(repo, supervisor)
        assert held["control_request"] == "pause" and held["control_status"] == "applied"
        assert Worker(queue).run_once() is None
        assert len(rows(queue, "collaboration_recorded_steps")) == 2
        if ending == "resume":
            queue.request_control(
                repo, supervisor, "resume", expected_version=held["state_version"]
            )
        else:
            controls.request(
                repo,
                group,
                CollaborationControlRequest(action="cancel", expected_revision=2),
                actor=ACTOR,
                key="cancel-group",
            )
        drain(queue)
    assert service.summary(repo, group)["status"] == (
        "completed" if ending == "resume" else "cancelled"
    )
    calls = rows(queue, "external_calls")
    reused = [call for call in calls if call["origin_call_id"] is not None]
    assert len(calls) - len(reused) == 20 and len(reused) == (20 if ending == "resume" else 0)
    assert len(rows(queue, "collaboration_recorded_steps")) == (72 if ending == "resume" else 2)
    proof(
        request,
        ending=ending,
        target_group=str(group),
        preserved_individual_pause=True,
        physical_source_calls=20,
        target_reused_calls=len(reused),
    )
