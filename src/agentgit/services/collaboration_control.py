"""协作组原键开始及有界 DAG 唤醒，不执行模型或工具。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.services.collaboration_exchange import TERMINAL, wake_collaboration
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService

MAX_GROUPS_PER_TICK = 100
FAILED_STATUSES = ("failed", "cancelled", "budget_exhausted", "outcome_unknown")


def wake_stopped(connection: Connection, repository: UUID, group_id: UUID) -> None:
    tasks, runs, jobs = (TABLES[n] for n in ("collaboration_tasks", "runs", "jobs"))
    rows = (
        connection.execute(
            sa.select(jobs.c.id)
            .join(
                tasks,
                sa.and_(tasks.c.repository_id == jobs.c.repository_id, tasks.c.job_id == jobs.c.id),
            )
            .join(
                runs,
                sa.and_(runs.c.repository_id == tasks.c.repository_id, runs.c.id == tasks.c.run_id),
            )
            .where(
                tasks.c.repository_id == repository,
                tasks.c.collaboration_id == group_id,
                jobs.c.status == "paused",
                runs.c.status.not_in(TERMINAL),
            )
            .with_for_update(of=jobs, skip_locked=True)
        )
        .scalars()
        .all()
    )
    if rows:
        connection.execute(
            jobs.update()
            .where(jobs.c.id.in_(rows))
            .values(status="queued", available_at=sa.func.clock_timestamp())
        )


class CollaborationCoordinator:
    def __init__(self, queue: JobService):
        self.queue, self.history = queue, queue.history

    def start(self, repository: UUID, group_id: UUID, *, actor: str, key: str) -> dict[str, Any]:
        if not actor.strip() or not key.strip() or len(actor) > 200 or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "协作开始必须提供有界身份及原键")
        route = f"POST /repositories/{repository}/collaborations/{group_id}/start"
        digest = object_digest(
            ObjectType.BLOB, encode(dict(schema_version=1, collaboration_id=str(group_id)))
        )
        groups, idem, tasks = (
            TABLES[n] for n in ("collaborations", "idempotency_keys", "collaboration_tasks")
        )
        with self.history.engine.begin() as c:
            c.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(encode([actor, route, key]).decode(), 0)
                    )
                )
            )
            group = (
                c.execute(
                    sa.select(groups)
                    .where(groups.c.repository_id == repository, groups.c.id == group_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if group is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "协作组不可访问")
            if group["actor"] != actor:
                raise AgentGitError(ErrorCode.FORBIDDEN, "协作开始身份与准备操作者不符")
            cached = (
                c.execute(
                    sa.select(idem).where(
                        idem.c.actor == actor, idem.c.route == route, idem.c.key == key
                    )
                )
                .mappings()
                .first()
            )
            if cached is not None:
                if cached["request_digest"] != digest:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "开始原键不能更换输入")
                cached_result: dict[str, Any] = self.history.objects.get_json(
                    cached["result_ref"], ObjectType.BLOB
                )
                return cached_result
            if group["status"] != "prepared":
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "仅未开始的 prepared 协作可开始")
            c.execute(groups.update().where(groups.c.id == group_id).values(status="running"))
            supervisor = c.execute(
                sa.select(tasks.c.job_id).where(
                    tasks.c.repository_id == repository,
                    tasks.c.collaboration_id == group_id,
                    tasks.c.role == "supervisor",
                )
            ).scalar_one()
            result = dict(schema_version=1, collaboration_id=str(group_id), status="running")
            obj = self.history.objects.put_json(ObjectType.BLOB, result)
            self.history._ref(c, repository, obj, "idempotency", str(supervisor), "response")
            c.execute(
                idem.insert().values(
                    actor=actor,
                    route=route,
                    key=key,
                    repository_id=repository,
                    request_digest=digest,
                    result_ref=obj.id,
                    job_id=supervisor,
                )
            )
            wake_collaboration(self.queue, c, repository, group_id)
            self.history.fault("collaboration.start_projected")
        return result

    def tick(self) -> None:
        groups, tasks, runs, jobs = (
            TABLES[n] for n in ("collaborations", "collaboration_tasks", "runs", "jobs")
        )
        members = (
            sa.select(tasks.c.id)
            .join(
                runs,
                sa.and_(runs.c.repository_id == tasks.c.repository_id, runs.c.id == tasks.c.run_id),
            )
            .where(
                tasks.c.repository_id == groups.c.repository_id,
                tasks.c.collaboration_id == groups.c.id,
            )
        )
        failed_member = members.where(runs.c.status.in_(FAILED_STATUSES)).exists()
        pending_stop = (
            members.join(
                jobs,
                sa.and_(jobs.c.repository_id == tasks.c.repository_id, jobs.c.id == tasks.c.job_id),
            )
            .where(runs.c.status.not_in(TERMINAL), jobs.c.status == "paused")
            .exists()
        )

        def completed(role: str) -> sa.ColumnElement[bool]:
            dependency = tasks.alias("dependency_" + role)
            execution = runs.alias("completed_" + role)
            return (
                sa.select(dependency.c.id)
                .join(execution, execution.c.id == dependency.c.run_id)
                .where(
                    dependency.c.repository_id == groups.c.repository_id,
                    dependency.c.collaboration_id == groups.c.id,
                    dependency.c.role == role,
                    execution.c.status == "completed",
                )
                .correlate(groups)
                .exists()
            )

        def received(role: str) -> sa.ColumnElement[bool]:
            sender = tasks.alias("sender_" + role)
            messages = TABLES["collaboration_messages"]
            return (
                sa.select(messages.c.id)
                .join(sender, sender.c.run_id == messages.c.sender_run_id)
                .where(
                    sender.c.repository_id == groups.c.repository_id,
                    sender.c.collaboration_id == groups.c.id,
                    sender.c.role == role,
                    messages.c.repository_id == groups.c.repository_id,
                    messages.c.collaboration_id == groups.c.id,
                    messages.c.receiver_run_id == runs.c.id,
                )
                .correlate(groups, runs)
                .exists()
            )

        researcher_done, verifier_done, writer_done = (
            completed(role) for role in ("researcher", "verifier", "writer")
        )
        dispatch, evidence, verification, result = (
            received(role) for role in ("supervisor", "researcher", "verifier", "writer")
        )
        # 子任务可能在完成暂停检查点之后才释放 Job，补偿事务唤醒略过的窗口。
        runnable = (
            members.join(
                jobs,
                sa.and_(jobs.c.repository_id == tasks.c.repository_id, jobs.c.id == tasks.c.job_id),
            )
            .where(
                jobs.c.status == "paused",
                runs.c.status.not_in(TERMINAL),
                sa.or_(
                    runs.c.control_request.is_distinct_from("pause"),
                    runs.c.control_status.is_distinct_from("applied"),
                ),
                sa.or_(
                    sa.and_(tasks.c.role == "researcher", dispatch),
                    sa.and_(tasks.c.role == "verifier", researcher_done, dispatch, evidence),
                    sa.and_(
                        tasks.c.role == "writer",
                        researcher_done,
                        verifier_done,
                        dispatch,
                        evidence,
                        verification,
                    ),
                    sa.and_(
                        tasks.c.role == "supervisor",
                        sa.or_(runs.c.checkpoint_commit_id.is_(None), sa.and_(writer_done, result)),
                    ),
                ),
            )
            .exists()
        )
        with self.history.engine.begin() as c:
            rows = (
                c.execute(
                    sa.select(groups.c.repository_id, groups.c.id, groups.c.status)
                    .where(
                        sa.or_(
                            sa.and_(groups.c.status.in_(("running", "paused")), failed_member),
                            sa.and_(groups.c.status == "running", runnable),
                            sa.and_(groups.c.status.in_(FAILED_STATUSES), pending_stop),
                        )
                    )
                    .order_by(groups.c.created_at, groups.c.id)
                    .limit(MAX_GROUPS_PER_TICK)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            for row in rows:
                if row["status"] in ("running", "paused"):
                    failed = (
                        c.execute(
                            sa.select(runs.c.status, runs.c.id, runs.c.last_event_seq)
                            .join(
                                tasks,
                                sa.and_(
                                    tasks.c.repository_id == runs.c.repository_id,
                                    tasks.c.run_id == runs.c.id,
                                ),
                            )
                            .where(
                                tasks.c.repository_id == row["repository_id"],
                                tasks.c.collaboration_id == row["id"],
                                runs.c.status.in_(FAILED_STATUSES),
                            )
                            .order_by(tasks.c.role)
                            .limit(1)
                        )
                        .mappings()
                        .first()
                    )
                    if failed is not None:
                        c.execute(
                            groups.update()
                            .where(groups.c.id == row["id"])
                            .values(
                                status=failed["status"],
                                failure_run_id=failed["id"],
                                failure_event_seq=failed["last_event_seq"],
                            )
                        )
                        wake_stopped(c, row["repository_id"], row["id"])
                    elif row["status"] == "running":
                        wake_collaboration(self.queue, c, row["repository_id"], row["id"])
                else:
                    wake_stopped(c, row["repository_id"], row["id"])
