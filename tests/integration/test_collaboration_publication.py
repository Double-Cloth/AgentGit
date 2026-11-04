"""对象发布延迟不能占用项目历史锁，其他协作组仍能推进检查点。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event, get_native_id

import pytest
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, rows
from test_collaboration_scale import ACTOR, prepare

from agentgit.core.canonical import ObjectType
from agentgit.runtime.collaboration_run import CollaborationRoleRunner
from agentgit.services.collaboration_control import CollaborationCoordinator
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import LeaseHeartbeat, Worker

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("publication", ["message_id", "publisher_event"])
def test_slow_message_or_shared_cas_publication_does_not_block_other_group(
    history, tmp_path, monkeypatch, publication
):
    queue = JobService(ResearchService(history, tmp_path / "data"))
    repository = history.create_repository("对象延迟下的独立协作")
    groups = prepare(queue, [repository], 2)
    coordinator = CollaborationCoordinator(queue)
    main = history.branch(repository)
    for index, (_, group) in enumerate(groups):
        coordinator.start(repository, group, actor=ACTOR, key=f"publication-start-{index}")
    # 先领取两个 Supervisor，避免先唤醒的 Researcher 被准备阶段提前执行。
    leases = [queue.claim(f"prepare-supervisor-{index}") for index in range(2)]
    with ExitStack() as stack:
        for lease in leases:
            assert lease is not None
            stack.enter_context(LeaseHeartbeat(queue, lease))
        for lease in leases:
            assert lease is not None
            assert (
                CollaborationRoleRunner(queue, lease, fault=lambda _: None).run()["status"]
                == "paused"
            )
    entered, release = Event(), Event()
    publishing = {}
    original = history.objects.put

    def slow_publish(kind, payload, *args, **kwargs):
        if (
            get_native_id() == publishing.get("thread")
            and kind == ObjectType.BLOB
            and f'"{publication}":'.encode() in payload
            and not entered.is_set()
        ):
            value = json.loads(payload)
            publishing["run"] = (
                value["sender"]
                if publication == "message_id"
                else value["publisher_event"]["agent_run_id"]
            )
            entered.set()
            assert release.wait(15), "对象发布延迟没有按期解除"
        return original(kind, payload, *args, **kwargs)

    monkeypatch.setattr(history.objects, "put", slow_publish)

    def first_worker():
        publishing["thread"] = get_native_id()
        return Worker(queue, name="slow-publisher").run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_worker)
        try:
            assert entered.wait(15), "没有进入实际消息／共享对象发布"
            second = pool.submit(Worker(queue, name="independent-group").run_once)
            assert second.result(timeout=12)["status"] == "completed"
            assert not first.done() and not release.is_set()
            tasks = {str(t["run_id"]): t for t in rows(queue, "collaboration_tasks")}
            blocked_group = tasks[publishing["run"]]["collaboration_id"]
            messages = rows(queue, "collaboration_messages")
            assert len(messages) == 8
            assert sum(message["collaboration_id"] == blocked_group for message in messages) == 3
            assert history.branch(repository) == main
        finally:
            release.set()
        assert first.result(timeout=15)["status"] == "completed"
    drain(queue)
    assert all(group["status"] == "completed" for group in rows(queue, "collaborations"))
    assert len(rows(queue, "external_calls")) == 40
    assert len(rows(queue, "collaboration_messages")) == 14
    assert len(rows(queue, "collaboration_receipts")) == 14
    assert len(rows(queue, "collaboration_publications")) == 10
    assert history.branch(repository) == main
