"""操作者的有界协作查询；只读公开投影，不读取角色私有 notes。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import encode
from agentgit.core.collaboration import CollaborationMessage, RolePolicy, SharedArtifactHead
from agentgit.core.collaboration_execution import CollaborationRoleState
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import BudgetLimits, BudgetUsage
from agentgit.services.collaboration import CollaborationService
from agentgit.services.collaboration_controls import CollaborationControlService
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService


class CollaborationViewService:
    def __init__(self, queue: JobService):
        self.queue, self.history = queue, queue.history
        self.base = CollaborationService(queue)
        self.controls = CollaborationControlService(queue)

    @staticmethod
    def _limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise AgentGitError(ErrorCode.VALIDATION, "协作查询每页必须为 1–100 项")

    def authorize_run(self, repo: UUID, run: UUID, *, actor: str) -> None:
        tasks, groups = TABLES["collaboration_tasks"], TABLES["collaborations"]
        with self.history.engine.connect() as c:
            owner = c.execute(
                sa.select(groups.c.actor)
                .join(
                    tasks,
                    sa.and_(
                        tasks.c.repository_id == groups.c.repository_id,
                        tasks.c.collaboration_id == groups.c.id,
                    ),
                )
                .where(tasks.c.repository_id == repo, tasks.c.run_id == run)
            ).scalar_one_or_none()
        if owner is not None and owner != actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "协作角色只允许所属操作者访问")

    def authorize_commit(self, repo: UUID, commit: str, *, actor: str) -> None:
        snapshots, tasks, groups = (
            TABLES[n] for n in ("snapshots", "collaboration_tasks", "collaborations")
        )
        with self.history.engine.connect() as c:
            owners = (
                c.execute(
                    sa.select(groups.c.actor)
                    .join(
                        tasks,
                        sa.and_(
                            tasks.c.repository_id == groups.c.repository_id,
                            tasks.c.collaboration_id == groups.c.id,
                        ),
                    )
                    .join(
                        snapshots,
                        sa.and_(
                            snapshots.c.repository_id == tasks.c.repository_id,
                            snapshots.c.run_id == tasks.c.run_id,
                        ),
                    )
                    .where(snapshots.c.repository_id == repo, snapshots.c.commit_id == commit)
                )
                .scalars()
                .all()
            )
        if any(owner != actor for owner in owners):
            raise AgentGitError(ErrorCode.FORBIDDEN, "协作历史只允许所属操作者访问")

    def list(
        self, repo: UUID, *, actor: str, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        self._limit(limit)
        groups, links = TABLES["collaborations"], TABLES["collaboration_recordings"]
        with self.history.engine.connect() as c:
            if (
                c.execute(
                    sa.select(TABLES["repositories"].c.id).where(
                        TABLES["repositories"].c.id == repo
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
            query = (
                sa.select(groups.c.id, groups.c.status, links.c.source_collaboration_id)
                .outerjoin(
                    links,
                    sa.and_(
                        links.c.repository_id == groups.c.repository_id,
                        links.c.collaboration_id == groups.c.id,
                    ),
                )
                .where(groups.c.repository_id == repo, groups.c.actor == actor)
            )
            if after_id is not None:
                query = query.where(groups.c.id > after_id)
            rows = c.execute(query.order_by(groups.c.id).limit(limit + 1)).mappings().all()
        return dict(
            items=[
                dict(
                    collaboration_id=str(r["id"]),
                    status=r["status"],
                    mode="recorded" if r["source_collaboration_id"] else "live",
                    source_collaboration_id=str(r["source_collaboration_id"])
                    if r["source_collaboration_id"]
                    else None,
                )
                for r in rows[:limit]
            ],
            next_after_id=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        )

    def summary(self, repo: UUID, group: UUID, *, actor: str) -> dict[str, Any]:
        # 控制查询在任何 CAS 读取前核对本人/项目，不把收据 accepted 当作静止。
        status = self.controls.status(repo, group, actor=actor)
        summary = self.base.summary(repo, group)
        tasks = {r["run_id"]: r for r in summary["roles"]}
        return {
            **summary,
            **status,
            "roles": [{**tasks[r["run_id"]], **r} for r in status["roles"]],
        }

    def role(self, repo: UUID, run: UUID, *, actor: str) -> dict[str, Any]:
        self.authorize_run(repo, run, actor=actor)
        tasks = TABLES["collaboration_tasks"]
        with self.history.engine.connect() as c:
            task = (
                c.execute(
                    sa.select(tasks).where(tasks.c.repository_id == repo, tasks.c.run_id == run)
                )
                .mappings()
                .first()
            )
            if task is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "协作角色不可访问")
            group = self.controls._scope(c, repo, task["collaboration_id"], actor)
            policy = parse_contract(
                self.base._owned(
                    c, repo, task["policy_ref"], "collaboration_task", str(task["id"]), "policy"
                ),
                RolePolicy,
            )
            limits = parse_contract(
                self.base._owned(
                    c, repo, task["budget_ref"], "collaboration_task", str(task["id"]), "budget"
                ),
                BudgetLimits,
            )
            if policy.role.value != task["role"] or policy.limits != limits:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "角色预算/权限绑定不符")
            calls = TABLES["external_calls"]
            counts = (
                c.execute(
                    sa.select(
                        sa.func.count().filter(calls.c.origin_call_id.is_(None)).label("physical"),
                        sa.func.count().filter(calls.c.origin_call_id.is_not(None)).label("reused"),
                        sa.func.count()
                        .filter(calls.c.status == "outcome_unknown")
                        .label("unknown"),
                    ).where(calls.c.repository_id == repo, calls.c.run_id == run)
                )
                .mappings()
                .one()
            )
            links = TABLES["collaboration_recordings"]
            source = c.execute(
                sa.select(links.c.source_collaboration_id).where(
                    links.c.repository_id == repo, links.c.collaboration_id == group["id"]
                )
            ).scalar_one_or_none()
        row = self.queue.run(repo, run)
        state = None
        if row["checkpoint_commit_id"] is not None:
            restored = self.history.restore(repo, row["checkpoint_commit_id"])
            state = parse_contract(encode(restored.data), CollaborationRoleState)
            if (
                state.collaboration_id,
                state.role.value,
                state.program_ref,
                state.private_input_ref,
            ) != (str(group["id"]), task["role"], group["program_ref"], task["private_input_ref"]):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "角色检查点与实际绑定不符")
        return dict(
            kind="collaboration_role/v1",
            collaboration_id=str(group["id"]),
            role=task["role"],
            program_ref=group["program_ref"],
            group_status=group["status"],
            mode=row["mode"],
            source_collaboration_id=str(source) if source else None,
            source_run_id=str(row["origin_run_id"]) if row["origin_run_id"] else None,
            phase=state.phase if state else "prepared",
            checkpoint_status=state.status.value if state else None,
            limits=limits.model_dump(mode="json"),
            permissions=list(policy.permissions),
            logical_usage=(state.usage if state else BudgetUsage()).model_dump(mode="json"),
            physical_call_intents=counts["physical"],
            reused_calls=counts["reused"],
            unknown_calls=counts["unknown"],
            has_model=True,
        )

    def messages(
        self, repo: UUID, group: UUID, *, actor: str, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        self._limit(limit)
        table, receipts = TABLES["collaboration_messages"], TABLES["collaboration_receipts"]
        with self.history.engine.connect() as c:
            self.controls._scope(c, repo, group, actor)
            query = (
                sa.select(table, receipts.c.received_commit_id, receipts.c.received_seq)
                .outerjoin(
                    receipts,
                    sa.and_(
                        receipts.c.repository_id == table.c.repository_id,
                        receipts.c.message_id == table.c.id,
                    ),
                )
                .where(table.c.repository_id == repo, table.c.collaboration_id == group)
            )
            if after_id is not None:
                query = query.where(table.c.id > after_id)
            rows = c.execute(query.order_by(table.c.id).limit(limit + 1)).mappings().all()
            items = []
            for r in rows[:limit]:
                envelope = parse_contract(
                    self.base._owned(
                        c,
                        repo,
                        r["envelope_ref"],
                        "collaboration_message",
                        str(r["id"]),
                        "envelope",
                    ),
                    CollaborationMessage,
                )
                if (
                    envelope.repository_id,
                    envelope.collaboration_id,
                    envelope.message_id,
                    envelope.sender,
                    envelope.receiver,
                    envelope.task_id,
                    envelope.payload_ref,
                ) != (
                    str(repo),
                    str(group),
                    str(r["id"]),
                    str(r["sender_run_id"]),
                    str(r["receiver_run_id"]),
                    str(r["receiver_task_id"]),
                    r["payload_ref"],
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "消息索引与所属信封不符")
                items.append(
                    dict(
                        **envelope.model_dump(mode="json"),
                        sent_commit_id=r["sent_commit_id"],
                        sent_seq=r["sent_seq"],
                        received_commit_id=r["received_commit_id"],
                        received_seq=r["received_seq"],
                    )
                )
        return dict(
            items=items, next_after_id=str(rows[limit - 1]["id"]) if len(rows) > limit else None
        )

    def publications(
        self,
        repo: UUID,
        group: UUID,
        *,
        actor: str,
        after_path: str | None = None,
        after_version: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        self._limit(limit)
        if (after_path is None) != (after_version is None) or (
            after_path is not None
            and (
                after_path
                not in ("evidence.json", "verification.json", "report.md", "sources.json")
                or after_version is None
                or not 1 <= after_version <= 9_007_199_254_740_991
            )
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "共享版本游标必须同时提供合法路径和正版本")
        table = TABLES["collaboration_publications"]
        with self.history.engine.connect() as c:
            scope = self.controls._scope(c, repo, group, actor)
            query = sa.select(table).where(
                table.c.repository_id == repo, table.c.collaboration_id == group
            )
            if after_path is not None:
                query = query.where(
                    sa.tuple_(table.c.path, table.c.version) > (after_path, after_version)
                )
            rows = (
                c.execute(query.order_by(table.c.path, table.c.version).limit(limit + 1))
                .mappings()
                .all()
            )
            items = []
            for r in rows[:limit]:
                head = parse_contract(
                    self.base._owned(
                        c,
                        repo,
                        r["head_ref"],
                        "collaboration_share",
                        str(group),
                        f"head:{r['path']}:{r['version']}",
                    ),
                    SharedArtifactHead,
                )
                if (
                    head.repository_id,
                    head.collaboration_id,
                    head.program_ref,
                    head.bindings_ref,
                    head.path,
                    head.version,
                    head.object_ref,
                    head.publisher_event.agent_run_id,
                ) != (
                    str(repo),
                    str(group),
                    scope["program_ref"],
                    scope["bindings_ref"],
                    r["path"],
                    r["version"],
                    r["object_ref"],
                    str(r["publisher_run_id"]),
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "共享索引与所属版本不符")
                items.append(
                    dict(
                        **head.model_dump(mode="json"),
                        head_ref=r["head_ref"],
                        commit_id=r["commit_id"],
                        event_seq=r["event_seq"],
                    )
                )
        return dict(
            items=items,
            next_after=dict(path=rows[limit - 1]["path"], version=rows[limit - 1]["version"])
            if len(rows) > limit
            else None,
        )
