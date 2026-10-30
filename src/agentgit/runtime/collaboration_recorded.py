"""Recorded 角色租约边界及准备态取消；成功轨迹耐久执行仍待接线。"""

from typing import Any

import sqlalchemy as sa

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import RunStatus
from agentgit.core.research import evolve
from agentgit.services.collaboration_exchange import CollaborationExchange
from agentgit.services.history import TABLES, EventInput
from agentgit.services.jobs import JobService


class CollaborationRecordedRoleRunner:
    def __init__(self, queue: JobService, lease: Lease):
        self.queue, self.lease = queue, lease
        self.exchange = CollaborationExchange(queue, lease)
        if self.exchange.payload.recording_ref is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 角色缺少固定来源")

    def run(self) -> dict[str, Any]:
        run = self.queue.run(self.lease.repository_id, self.lease.run_id)
        if run["status"] == "cancelled" and run["checkpoint_commit_id"] is not None:
            if self.exchange.load().status != RunStatus.CANCELLED:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 取消终点与 Run 不符")
            self.queue.finish(self.lease, "cancelled")
            return self.summary()
        if (
            run["checkpoint_commit_id"] is not None
            or self.queue.apply_control(self.lease) != "cancel"
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Recorded 成功轨迹 Worker 尚未接通")
        with self.queue.history.engine.connect() as c:
            groups = TABLES["collaborations"]
            group = (
                c.execute(
                    sa.select(groups).where(
                        groups.c.repository_id == self.lease.repository_id,
                        groups.c.id == self.exchange.group_id,
                    )
                )
                .mappings()
                .one()
            )
        if group["status"] != "cancelled" or group["failure_run_id"] is None:
            raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "准备态 Recorded 仅处理确切组取消")
        events = (
            ()
            if group["failure_run_id"] == self.lease.run_id
            else (
                EventInput(
                    "collaboration.parent_stopped",
                    "collaboration",
                    dict(collaboration_id=str(self.exchange.group_id)),
                    caused_by_run_id=group["failure_run_id"],
                    caused_by_seq=group["failure_event_seq"],
                ),
            )
        )
        state = evolve(self.exchange.load(), status=RunStatus.CANCELLED, phase="done")
        self.exchange.save(state, "collaboration.stopped", apply_control=True, events=events)
        self.queue.finish(self.lease, "cancelled")
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return dict(
            job_id=str(self.lease.job_id), run_id=str(self.lease.run_id), status="cancelled"
        )
