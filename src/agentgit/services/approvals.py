"""审批是操作元数据；等待不改变精确动作绑定的逻辑 checkpoint。"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.effects import BoundAction, ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallOccurrence, EffectClass
from agentgit.core.snapshots import EventPayload
from agentgit.services.history import TABLES, EventInput, HistoryService


class ApprovalService:
    def __init__(
        self, history: HistoryService, policy: ToolPolicy, *, actor: str, ttl_seconds: int = 300
    ):
        if not actor or len(actor) > 200 or not 1 <= ttl_seconds <= 86400:
            raise AgentGitError(ErrorCode.CONFIGURATION, "审批操作者或有效期无效")
        self.history, self.policy, self.actor, self.ttl_seconds = (
            history,
            policy,
            actor,
            ttl_seconds,
        )

    def list_for_run(self, repository_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
        approvals = TABLES["approvals"]
        with self.history.engine.connect() as connection:
            self.history._run(connection, repository_id, run_id)
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(approvals)
                    .where(approvals.c.repository_id == repository_id, approvals.c.run_id == run_id)
                    .order_by(approvals.c.created_at, approvals.c.id)
                    .limit(100)
                ).mappings()
            ]

    @staticmethod
    def expire_pending(history: HistoryService, *, limit: int = 100) -> int:
        """有界回收等待中的过期审批，交给 Worker 落实失败或取消。"""
        table, runs, branches, jobs = (
            TABLES["approvals"],
            TABLES["runs"],
            TABLES["branches"],
            TABLES["jobs"],
        )
        if not 1 <= limit <= 1000:
            raise AgentGitError(ErrorCode.VALIDATION, "审批到期扫描上限无效")
        with history.engine.connect() as connection:
            candidates = list(
                connection.execute(
                    sa.select(table.c.id, table.c.repository_id, table.c.run_id)
                    .join(runs, runs.c.id == table.c.run_id)
                    .where(
                        table.c.status.in_(("pending", "approved")),
                        table.c.expires_at <= sa.func.clock_timestamp(),
                        runs.c.status == "waiting_approval",
                    )
                    .order_by(table.c.expires_at, table.c.id)
                    .limit(limit)
                )
            )
        expired = 0
        for candidate in candidates:
            with history.engine.begin() as connection:
                run = history._run(connection, candidate.repository_id, candidate.run_id, lock=True)
                connection.execute(
                    sa.select(branches.c.name)
                    .where(
                        branches.c.repository_id == candidate.repository_id,
                        branches.c.name == run["branch_name"],
                    )
                    .with_for_update()
                ).first()
                connection.execute(
                    sa.select(jobs.c.id).where(jobs.c.run_id == candidate.run_id).with_for_update()
                ).first()
                row = (
                    connection.execute(
                        sa.select(table).where(table.c.id == candidate.id).with_for_update()
                    )
                    .mappings()
                    .one()
                )
                now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
                if (
                    run["status"] != "waiting_approval"
                    or row["status"] not in ("pending", "approved")
                    or row["expires_at"] > now
                    or row["checkpoint_commit_id"] != run["checkpoint_commit_id"]
                ):
                    continue
                connection.execute(
                    table.update().where(table.c.id == row["id"]).values(status="expired")
                )
                event = EventInput(
                    "approval.expired",
                    "approval",
                    {"approval_id": str(row["id"]), "actor": "system"},
                )
                payload = history.objects.put_json(
                    ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
                )
                seq = int(run["last_event_seq"]) + 1
                history._event(
                    connection, candidate.repository_id, candidate.run_id, seq, event, payload
                )
                connection.execute(
                    runs.update()
                    .where(runs.c.id == candidate.run_id)
                    .values(
                        status=row["resume_status"],
                        failure_code=None,
                        last_event_seq=seq,
                    )
                )
                connection.execute(
                    jobs.update()
                    .where(jobs.c.run_id == candidate.run_id)
                    .values(
                        status="queued",
                        available_at=now,
                        worker=None,
                        lease_until=None,
                        last_error=None,
                    )
                )
                expired += 1
        return expired

    def admit(
        self,
        connection: Connection,
        run: RowMapping,
        occurrence: CallOccurrence,
        effect: EffectClass,
        max_bytes: int,
        call_id: UUID,
    ) -> AgentGitError | None:
        """调用者持 Run→Branch→Job 锁；返回拒绝值以保留等待记录的事务。"""
        rule = self.policy.rule(occurrence, effect)
        if rule.preauthorized:
            if run["status"] == "waiting_approval":
                return AgentGitError(ErrorCode.VERSION_CONFLICT, "Run 正在等待另一项动作审批")
            return None
        action = BoundAction(
            repository_id=str(run["repository_id"]),
            run_id=str(run["id"]),
            branch_name=run["branch_name"],
            checkpoint=run["checkpoint_commit_id"],
            occurrence=occurrence,
            effect=effect,
            policy_digest=self.policy.digest(),
            actor=self.actor,
            max_response_bytes=max_bytes,
        )
        table = TABLES["approvals"]
        row = (
            connection.execute(
                sa.select(table)
                .where(table.c.run_id == run["id"], table.c.call_digest == action.digest())
                .with_for_update()
            )
            .mappings()
            .first()
        )
        now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        if row is not None:
            if row["expires_at"] <= now and row["status"] in ("pending", "approved"):
                connection.execute(
                    table.update().where(table.c.id == row["id"]).values(status="expired")
                )
                return AgentGitError(ErrorCode.FORBIDDEN, "审批已经过期，动作没有启动")
            if row["status"] == "approved":
                connection.execute(
                    table.update()
                    .where(table.c.id == row["id"])
                    .values(status="consumed", consumed_at=now, consumed_call_id=call_id)
                )
                return None
            if row["status"] != "pending":
                return AgentGitError(ErrorCode.FORBIDDEN, "审批已拒绝、过期或被消费")
        else:
            if run["status"] == "waiting_approval":
                return AgentGitError(ErrorCode.VERSION_CONFLICT, "已有待审批动作，不能替换")
            approval_id = uuid4()
            policy_obj = self.history.objects.put(
                ObjectType.BLOB, encode(self.policy.model_dump(mode="json"))
            )
            action_obj = self.history.objects.put(
                ObjectType.CALL, encode(action.model_dump(mode="json"))
            )
            for role, obj in (("policy", policy_obj), ("action", action_obj)):
                self.history._ref(
                    connection, run["repository_id"], obj, "approval", str(approval_id), role
                )
            # 请求原文必须可审查；仅凭摘要不足以进行人工批准。
            request = self.history.objects.get(occurrence.request_digest, ObjectType.BLOB)
            request_obj = self.history.objects.put(ObjectType.BLOB, request)
            self.history._ref(
                connection,
                run["repository_id"],
                request_obj,
                "approval",
                str(approval_id),
                "request",
            )
            connection.execute(
                table.insert().values(
                    id=approval_id,
                    repository_id=run["repository_id"],
                    run_id=run["id"],
                    checkpoint_commit_id=action.checkpoint,
                    call_digest=action.digest(),
                    action_ref=action_obj.id,
                    policy_digest=policy_obj.id,
                    actor=self.actor,
                    resume_status=run["status"],
                    expires_at=now + timedelta(seconds=self.ttl_seconds),
                )
            )
            self._event(
                connection,
                run,
                "approval.requested",
                {"approval_id": str(approval_id), "call_digest": action.digest()},
            )
        connection.execute(
            TABLES["runs"]
            .update()
            .where(TABLES["runs"].c.id == run["id"])
            .values(status="waiting_approval", failure_code=ErrorCode.APPROVAL_REQUIRED.value)
        )
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.run_id == run["id"])
            .values(
                status="waiting_approval",
                worker=None,
                lease_until=None,
                last_error=ErrorCode.APPROVAL_REQUIRED.value,
            )
        )
        return AgentGitError(ErrorCode.APPROVAL_REQUIRED, "动作已登记，等待精确审批")

    def decide(
        self,
        repository_id: UUID,
        approval_id: UUID,
        *,
        actor: str,
        decision: str,
        expected_version: int,
    ) -> dict[str, Any]:
        if decision not in ("approve", "reject"):
            raise AgentGitError(ErrorCode.VALIDATION, "未知审批决定")
        table = TABLES["approvals"]
        failure = None
        with self.history.engine.begin() as connection:
            hint = connection.execute(
                sa.select(table.c.run_id).where(
                    table.c.id == approval_id, table.c.repository_id == repository_id
                )
            ).first()
            if hint is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "审批不可访问")
            run = self.history._run(connection, repository_id, hint.run_id, lock=True)
            branches, jobs = TABLES["branches"], TABLES["jobs"]
            branch = (
                connection.execute(
                    sa.select(branches)
                    .where(
                        branches.c.repository_id == repository_id,
                        branches.c.name == run["branch_name"],
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            connection.execute(
                sa.select(jobs.c.id).where(jobs.c.run_id == run["id"]).with_for_update()
            ).first()
            row = (
                connection.execute(
                    sa.select(table).where(table.c.id == approval_id).with_for_update()
                )
                .mappings()
                .one()
            )
            if actor != row["actor"] or actor != self.actor:
                raise AgentGitError(ErrorCode.FORBIDDEN, "操作者与精确审批绑定不符")
            if row["decision"] is not None:
                if row["decision"] != decision:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "审批已有相反决定")
                return dict(row)
            if (
                run["state_version"] != expected_version
                or run["status"] != "waiting_approval"
                or run["checkpoint_commit_id"] != row["checkpoint_commit_id"]
                or branch["head_commit_id"] != row["checkpoint_commit_id"]
                or branch["active_writer_run_id"] != run["id"]
                or row["policy_digest"] != self.policy.digest()
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "审批对应的状态或策略已改变")
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            if row["status"] != "pending" or row["expires_at"] <= now:
                connection.execute(
                    table.update().where(table.c.id == approval_id).values(status="expired")
                )
                failure = AgentGitError(ErrorCode.FORBIDDEN, "审批已过期，未批准动作")
                self._event(connection, run, "approval.expired", {"approval_id": str(approval_id)})
            else:
                connection.execute(
                    table.update()
                    .where(table.c.id == approval_id)
                    .values(
                        status="approved" if decision == "approve" else "rejected",
                        decision=decision,
                        decided_at=now,
                    )
                )
                self._event(
                    connection,
                    run,
                    "approval.decided",
                    {
                        "approval_id": str(approval_id),
                        "actor": actor,
                        "decision": decision,
                    },
                )
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == run["id"])
                .values(status=row["resume_status"], failure_code=None)
            )
            connection.execute(
                jobs.update()
                .where(jobs.c.run_id == run["id"])
                .values(
                    status="queued",
                    available_at=now,
                    worker=None,
                    lease_until=None,
                    last_error=None,
                )
            )
            result = dict(
                connection.execute(sa.select(table).where(table.c.id == approval_id))
                .mappings()
                .one()
            )
        if failure is not None:
            raise failure
        return result

    def _event(
        self, connection: Connection, run: RowMapping, kind: str, details: dict[str, Any]
    ) -> None:
        event = EventInput(kind, "approval", details)
        payload = self.history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=details).model_dump(mode="json")
        )
        # 同一事务可能先写过事件，读取当前序号而非使用旧 RowMapping。
        current = self.history._run(connection, run["repository_id"], run["id"])
        seq = int(current["last_event_seq"]) + 1
        self.history._event(connection, run["repository_id"], run["id"], seq, event, payload)
        connection.execute(
            TABLES["runs"]
            .update()
            .where(TABLES["runs"].c.id == run["id"])
            .values(last_event_seq=seq)
        )
