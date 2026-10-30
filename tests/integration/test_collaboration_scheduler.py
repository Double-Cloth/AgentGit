"""真实队列中，旧停止组或人工暂停组不占用新故障的有界传播窗口。"""

from uuid import UUID

import pytest
from test_collaboration_execution import PROGRAM, drain, rows, start
from test_collaboration_execution import archive as archive
from test_collaboration_execution import flow as flow

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.core.collaboration import CollaborationProgram, CollaborationRole
from agentgit.core.collaboration_runs import RolePrivateInput
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.runtime.collaboration_run import CollaborationRoleRunner
from agentgit.services import collaboration_control
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def fingerprint(queue, repo):
    return {
        name: [row for row in rows(queue, name) if row.get("repository_id") == repo]
        for name in ["runs", "jobs", "commits", "events", "external_calls", "collaborations"]
    }


@pytest.mark.parametrize("blocker", ["stopped", "paused"])
def test_scan_window_only_counts_groups_with_pending_scheduler_work(flow, monkeypatch, blocker):
    queue, service, coordinator, repo, receipt, roles = flow
    # 把窗口缩到 1，两个真实组即可复现历史组占满窗口的结构，不伪造运行行。
    monkeypatch.setattr(collaboration_control, "MAX_GROUPS_PER_TICK", 1)
    start(flow)
    if blocker == "paused":
        assert Worker(queue).run_once()["status"] == "paused"
        paused = []

        def pause(point):
            if point == "collaboration.after_saved_result" and not paused:
                target = UUID(roles["researcher"]["run_id"])
                queue.request_control(
                    repo, target, "pause", expected_version=queue.run(repo, target)["state_version"]
                )
                paused.append(True)

        assert Worker(queue, fault=pause).run_once()["status"] == "paused"
        assert paused
    else:
        target = UUID(roles["supervisor"]["run_id"])
        queue.request_control(
            repo, target, "cancel", expected_version=queue.run(repo, target)["state_version"]
        )
    drain(queue)
    old = fingerprint(queue, repo)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == (
        "running" if blocker == "paused" else "cancelled"
    )

    # 新组仍通过真实准备/开始入口；不改 Job 顺序、状态或历史字段。
    new_repo = queue.history.create_repository("有界扫描后的新故障")
    program = parse_contract(PROGRAM.read_bytes(), CollaborationProgram)
    prepared = service.prepare(
        new_repo,
        program,
        actor="collaboration-operator",
        key="prepare-new",
        private_inputs=tuple(
            RolePrivateInput(role=r, notes=("private-" + r.value,)) for r in CollaborationRole
        ),
    )
    group = UUID(prepared.collaboration_id)
    coordinator.start(new_repo, group, actor="collaboration-operator", key="start-new")
    assert Worker(queue).run_once()["status"] == "paused"

    def model(role):
        if role == "researcher":
            raise AgentGitError(ErrorCode.FORBIDDEN, "测试拒绝创建角色模型")
        return MockCollaborationModel()

    failed = Worker(queue, collaboration_model_factory=model).run_once()
    assert failed["status"] == "failed" and failed["error_code"] == "forbidden"
    assert service.summary(new_repo, group)["status"] == "running"
    coordinator.tick()
    current = service.summary(new_repo, group)
    assert current["status"] == "failed"
    drain(queue)
    assert fingerprint(queue, repo) == old
    summary = service.summary(new_repo, group)
    assert all(
        queue.run(new_repo, UUID(role["run_id"]))["status"] in ("failed", "cancelled")
        for role in summary["roles"]
    )
    own = [c for c in rows(queue, "external_calls") if c["repository_id"] == new_repo]
    assert len(own) == 1 and own[0]["provider"] == "llm:collaboration-role/v1"


def test_dependency_completing_between_pause_checkpoint_and_job_finish_is_reawakened(flow):
    queue, service, coordinator, repo, receipt, roles = flow
    start(flow)
    assert Worker(queue).run_once()["status"] == "paused"
    verifier = UUID(roles["verifier"]["run_id"])
    queue.request_control(
        repo, verifier, "resume", expected_version=queue.run(repo, verifier)["state_version"]
    )
    leases = [queue.claim("early-child-" + str(i)) for i in range(2)]
    assert all(lease is not None for lease in leases)
    by_run = {str(lease.run_id): lease for lease in leases}
    researcher = CollaborationRoleRunner(
        queue, by_run[roles["researcher"]["run_id"]], fault=lambda _: None
    )
    completed = []

    def after_pause(point):
        if point == "collaboration.stopped_saved" and not completed:
            # 前置条件尚未完成时先暂停；在 Job 仍被子任务持有时完成依赖。
            assert queue.get(repo, UUID(roles["verifier"]["job_id"]))["status"] == "running"
            assert researcher.run()["status"] == "completed"
            assert queue.get(repo, UUID(roles["verifier"]["job_id"]))["status"] == "running"
            completed.append(True)

    early = CollaborationRoleRunner(queue, by_run[str(verifier)], fault=after_pause)
    assert early.run()["status"] == "paused" and completed
    assert queue.get(repo, UUID(roles["verifier"]["job_id"]))["status"] == "paused"
    coordinator.tick()
    drain(queue)
    assert service.summary(repo, UUID(receipt.collaboration_id))["status"] == "completed"
    assert len(rows(queue, "external_calls")) == 20
    assert len(rows(queue, "collaboration_messages")) == 7
