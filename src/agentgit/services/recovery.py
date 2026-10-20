"""租约接管后的历史封存、写者交接和未决调用审计。"""

from dataclasses import replace
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS, Lease
from agentgit.core.snapshots import EventPayload
from agentgit.services.history import TABLES, EventInput
from agentgit.services.jobs import JobService


def recovery_origins(queue: JobService, lease: Lease) -> tuple[UUID, ...]:
    job = queue.get(lease.repository_id, lease.job_id)
    root = job["origin_run_id"]
    if root is None:
        return ()
    result: list[UUID] = []
    with queue.history.engine.connect() as connection:
        current = queue.history._run(connection, lease.repository_id, lease.run_id)["origin_run_id"]
        while current is not None:
            if current in result or len(result) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "恢复来源链含环或超出上限")
            result.append(current)
            if current == root:
                return tuple(result)
            current = queue.history._run(connection, lease.repository_id, current)["origin_run_id"]
    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "恢复来源链没有到达 Job 初始运行")


def prepare_recovery(queue: JobService, lease: Lease) -> Lease | None:
    history = queue.history
    interrupted = EventInput("run.interrupted", "recovery", {"reason": "lease_expired"})
    payload = history.objects.put_json(
        ObjectType.EVENT, EventPayload(data=interrupted.payload).model_dump(mode="json")
    )
    jobs, runs, branches = TABLES["jobs"], TABLES["runs"], TABLES["branches"]
    with history.engine.begin() as connection:
        old = history._run(connection, lease.repository_id, lease.run_id, lock=True)
        connection.execute(
            sa.select(branches.c.name)
            .where(
                branches.c.repository_id == lease.repository_id,
                branches.c.name == old["branch_name"],
            )
            .with_for_update()
        )
        job = queue.guard(connection, lease, lease.run_id)
        if old["status"] in TERMINAL_RUNS or old["status"] in (
            "paused",
            "waiting_approval",
            "outcome_unknown",
        ):
            connection.execute(
                jobs.update()
                .where(jobs.c.id == lease.job_id)
                .values(
                    status=old["status"],
                    worker=None,
                    lease_until=None,
                    checkpoint_commit_id=old["checkpoint_commit_id"],
                    last_error=old["failure_code"],
                )
            )
            return None
        origins = [lease.run_id]
        root = job["origin_run_id"]
        current = old["origin_run_id"]
        while root is not None and origins[-1] != root:
            if current is None or current in origins or len(origins) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "恢复来源链非法")
            origins.append(current)
            current = history._run(connection, lease.repository_id, current)["origin_run_id"]
        connection.execute(
            runs.update().where(runs.c.id == lease.run_id).values(status="interrupted")
        )
        seq = int(old["last_event_seq"]) + 1
        history._event(connection, lease.repository_id, lease.run_id, seq, interrupted, payload)
        connection.execute(
            runs.update().where(runs.c.id == lease.run_id).values(last_event_seq=seq)
        )
        connection.execute(
            branches.update()
            .where(
                branches.c.repository_id == lease.repository_id,
                branches.c.name == old["branch_name"],
                branches.c.active_writer_run_id == lease.run_id,
            )
            .values(active_writer_run_id=None)
        )
        new_run = uuid4()
        connection.execute(
            runs.insert().values(
                id=new_run,
                repository_id=lease.repository_id,
                task_id=old["task_id"],
                agent_config_id=old["agent_config_id"],
                branch_name=old["branch_name"],
                base_commit_id=old["checkpoint_commit_id"],
                checkpoint_commit_id=old["checkpoint_commit_id"],
                origin_run_id=lease.run_id,
                mode=old["mode"],
                status="queued",
                control_request=old["control_request"],
                control_status=old["control_status"],
            )
        )
        tasks = TABLES["tasks"]
        changed = connection.execute(
            tasks.update()
            .where(
                tasks.c.repository_id == lease.repository_id,
                tasks.c.id == old["task_id"],
                tasks.c.active_run_id == lease.run_id,
            )
            .values(active_run_id=new_run)
            .returning(tasks.c.id)
        ).first()
        if changed is None:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "恢复时任务活动 Run 已改变")
        connection.execute(
            jobs.update()
            .where(jobs.c.id == lease.job_id)
            .values(
                run_id=new_run,
                origin_run_id=job["origin_run_id"] or lease.run_id,
                checkpoint_commit_id=old["checkpoint_commit_id"],
            )
        )
    return replace(lease, run_id=new_run, recovery_required=False)
