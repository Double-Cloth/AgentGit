"""PostgreSQL 调度、请求幂等与租约；领取事务不执行 Agent。"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS, JobPayload, Lease, Submission
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, BudgetLimits
from agentgit.core.snapshots import EventPayload
from agentgit.core.versions import LiveReplaySpec, RecordedReplaySpec
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES, EventInput
from agentgit.services.research import ResearchService
from agentgit.storage.database import transaction


class JobService:
    def __init__(self, research: ResearchService, *, lease_ms: int = 15_000):
        if not 100 <= lease_ms <= 120_000:
            raise AgentGitError(ErrorCode.CONFIGURATION, "租约必须为 100–120000 毫秒")
        self.research, self.history, self.lease_ms = research, research.history, lease_ms

    def enqueue(
        self,
        repository_id: UUID,
        task_id: UUID,
        *,
        actor: str,
        key: str,
        config_version: int,
        expected_branch_version: int,
        expected_head: str | None,
        branch_name: str = "main",
        budget: BudgetLimits | None = None,
        replay: RecordedReplaySpec | LiveReplaySpec | None = None,
        connection: Connection | None = None,
    ) -> Submission:
        if not actor or not key or len(actor) > 200 or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "操作者和幂等键必须为有界非空字符串")
        route = f"POST /tasks/{task_id}/runs"
        submission_connection = connection
        request_payload = {
            "schema_version": 1,
            "repository_id": str(repository_id),
            "task_id": str(task_id),
            "config_version": config_version,
            "head": expected_head,
            "branch_version": expected_branch_version,
            "branch_name": branch_name,
            "budget": budget.model_dump(mode="json") if budget is not None else None,
        }
        if replay is not None:
            request_payload["replay"] = replay.model_dump(mode="json")
        request_digest = object_digest(
            ObjectType.BLOB,
            encode(request_payload),
        )
        tasks, configs, nodes = TABLES["tasks"], TABLES["agent_configs"], TABLES["task_nodes"]
        with transaction(self.history.engine, submission_connection) as connection:
            task = (
                connection.execute(
                    sa.select(tasks).where(
                        tasks.c.repository_id == repository_id, tasks.c.id == task_id
                    )
                )
                .mappings()
                .first()
            )
            if task is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "任务不可访问")
            pinned_config = (
                connection.execute(
                    sa.select(configs).where(
                        configs.c.id == task["agent_config_id"],
                        configs.c.repository_id == repository_id,
                    )
                )
                .mappings()
                .one()
            )
            plan_version = connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(nodes.c.plan_version), 0)).where(
                    nodes.c.task_id == task_id
                )
            ).scalar_one()
        config = self.research.config(
            repository_id, task["agent_config_id"], connection=submission_connection
        )
        task_budget = parse_contract(
            self.history.objects.get(task["budget_ref"], ObjectType.BLOB), BudgetLimits
        )
        limits = budget or task_budget
        if not limits.within(task_budget, config.limits):
            raise AgentGitError(ErrorCode.FORBIDDEN, "Run 预算不能扩大 Task 或配置上限")
        parse_contract(
            encode(
                {
                    **config.model_dump(mode="json"),
                    "limits": limits.model_dump(mode="json"),
                }
            ),
            AgentConfig,
        )
        payload = JobPayload(
            replay=replay,
            config_id=str(task["agent_config_id"]),
            goal_ref=task["goal_ref"],
            code_revision=source_revision(),
            corpus_digest=self.research.corpus.digest(),
            sources=tuple(self.research.corpus.sources.values()),
            evaluation_time=self.research.corpus.evaluation_time,
            initial_plan_version=plan_version,
            max_wall_ms=limits.max_wall_ms,
            limits=limits,
        )
        job_id, run_id = uuid4(), uuid4()
        submitted = Submission(job_id=str(job_id), run_id=str(run_id))
        payload_obj = self.history.objects.put(
            ObjectType.BLOB, encode(payload.model_dump(mode="json"))
        )
        response_obj = self.history.objects.put(
            ObjectType.BLOB, encode(submitted.model_dump(mode="json"))
        )
        idem, branches, runs = TABLES["idempotency_keys"], TABLES["branches"], TABLES["runs"]
        with transaction(self.history.engine, submission_connection) as connection:
            scope = encode([actor, route, key]).decode("utf-8")
            connection.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(scope, 0)))
            )
            cached = (
                connection.execute(
                    sa.select(idem).where(
                        idem.c.actor == actor, idem.c.route == route, idem.c.key == key
                    )
                )
                .mappings()
                .first()
            )
            if cached:
                if (
                    cached["request_digest"] != request_digest
                    or cached["repository_id"] != repository_id
                ):
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一幂等键不能用于不同请求")
                return parse_contract(
                    self.history.objects.get(cached["result_ref"], ObjectType.BLOB), Submission
                )
            current = (
                connection.execute(
                    sa.select(tasks)
                    .where(tasks.c.id == task_id, tasks.c.repository_id == repository_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                current["agent_config_id"] != task["agent_config_id"]
                or current["goal_ref"] != task["goal_ref"]
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "任务输入在提交期间已改变")
            current_config = (
                connection.execute(
                    sa.select(configs)
                    .where(configs.c.id == current["agent_config_id"])
                    .with_for_update(read=True)
                )
                .mappings()
                .one()
            )
            if (
                current_config["version"] != config_version
                or current_config["config_ref"] != pinned_config["config_ref"]
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "配置版本在提交前已改变")
            if current["active_run_id"]:
                status = connection.execute(
                    sa.select(runs.c.status).where(runs.c.id == current["active_run_id"])
                ).scalar_one()
                if status not in TERMINAL_RUNS:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "任务已有活动 Run")
            latest_plan = connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(nodes.c.plan_version), 0)).where(
                    nodes.c.task_id == task_id
                )
            ).scalar_one()
            if latest_plan != plan_version:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "计划版本在提交期间已改变")
            branch = (
                connection.execute(
                    sa.select(branches)
                    .where(
                        branches.c.repository_id == repository_id, branches.c.name == branch_name
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if branch is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "分支不可访问")
            if (
                branch["active_writer_run_id"] is not None
                or branch["head_commit_id"] != expected_head
                or branch["version"] != expected_branch_version
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "分支已被占用或版本已改变")
            forks = TABLES["branch_forks"]
            fork = (
                connection.execute(
                    sa.select(forks).where(
                        forks.c.repository_id == repository_id, forks.c.branch_name == branch_name
                    )
                )
                .mappings()
                .first()
            )
            if replay is None and fork is not None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "历史分支必须通过 Replay 明确选择执行模式")
            if replay is not None and (
                fork is None
                or fork["source_run_id"] != UUID(replay.source_run_id)
                or fork["from_commit_id"] != replay.from_commit
                or expected_head != replay.from_commit
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "重放 Job 必须绑定相同来源的独立分支")
            connection.execute(
                runs.insert().values(
                    id=run_id,
                    repository_id=repository_id,
                    task_id=task_id,
                    agent_config_id=current["agent_config_id"],
                    branch_name=branch_name,
                    base_commit_id=expected_head,
                    checkpoint_commit_id=expected_head,
                    origin_run_id=UUID(replay.source_run_id)
                    if replay
                    else current["active_run_id"],
                    mode=replay.mode if replay else "live",
                    status="queued",
                )
            )
            connection.execute(
                tasks.update().where(tasks.c.id == task_id).values(active_run_id=run_id)
            )
            self.history._ref(connection, repository_id, payload_obj, "job", str(job_id), "payload")
            self.history._ref(
                connection, repository_id, response_obj, "idempotency", str(job_id), "response"
            )
            connection.execute(
                TABLES["jobs"]
                .insert()
                .values(
                    id=job_id,
                    repository_id=repository_id,
                    run_id=run_id,
                    kind="research_run",
                    payload_ref=payload_obj.id,
                )
            )
            connection.execute(
                idem.insert().values(
                    actor=actor,
                    route=route,
                    key=key,
                    repository_id=repository_id,
                    request_digest=request_digest,
                    result_ref=response_obj.id,
                    job_id=job_id,
                )
            )
        return submitted

    def get(self, repository_id: UUID, job_id: UUID) -> dict[str, Any]:
        jobs = TABLES["jobs"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.repository_id == repository_id, jobs.c.id == job_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "Job 不可访问")
        return dict(row)

    def run(self, repository_id: UUID, run_id: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            return dict(self.history._run(connection, repository_id, run_id))

    def payload(self, job: dict[str, Any] | RowMapping) -> JobPayload:
        return parse_contract(
            self.history.objects.get(job["payload_ref"], ObjectType.BLOB), JobPayload
        )

    def claim(self, worker: str) -> Lease | None:
        if not worker or len(worker) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "Worker 身份非法")
        jobs = TABLES["jobs"]
        executions = TABLES["sandbox_executions"]
        with self.history.engine.begin() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            candidate = (
                connection.execute(
                    sa.select(jobs)
                    .where(
                        sa.or_(
                            sa.and_(jobs.c.status == "queued", jobs.c.available_at <= now),
                            sa.and_(jobs.c.status == "running", jobs.c.lease_until <= now),
                            sa.and_(
                                jobs.c.status == "outcome_unknown",
                                jobs.c.available_at <= now,
                                sa.exists().where(
                                    executions.c.job_id == jobs.c.id,
                                    executions.c.repository_id == jobs.c.repository_id,
                                    executions.c.result_ref.is_(None),
                                    executions.c.cleanup_confirmed.is_(False),
                                ),
                            ),
                        )
                    )
                    .order_by(jobs.c.created_at, jobs.c.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if candidate is None:
                return None
            payload = self.payload(candidate)
            token = int(candidate["fencing_token"]) + 1
            connection.execute(
                jobs.update()
                .where(jobs.c.id == candidate["id"])
                .values(
                    status="running",
                    worker=worker,
                    fencing_token=token,
                    attempt=candidate["attempt"] + 1,
                    heartbeat=now,
                    lease_until=now + timedelta(milliseconds=self.lease_ms),
                    started_at=candidate["started_at"] or now,
                    deadline_at=candidate["deadline_at"]
                    or now + timedelta(milliseconds=payload.max_wall_ms),
                )
            )
            return Lease(
                candidate["id"],
                candidate["repository_id"],
                candidate["run_id"],
                worker,
                token,
                self.lease_ms,
                candidate["status"] in ("running", "outcome_unknown"),
            )

    def guard(self, connection: Connection, lease: Lease, run_id: UUID | None = None) -> RowMapping:
        connection.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
        jobs = TABLES["jobs"]
        row = (
            connection.execute(
                sa.select(jobs)
                .where(jobs.c.id == lease.job_id, jobs.c.repository_id == lease.repository_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        if (
            row is None
            or row["status"] != "running"
            or row["worker"] != lease.worker
            or row["fencing_token"] != lease.fencing_token
            or row["lease_until"] <= now
            or (run_id is not None and row["run_id"] != run_id)
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "Job 租约、fencing token 或 Run 已失效")
        return row

    def heartbeat(self, lease: Lease) -> None:
        with self.history.engine.begin() as connection:
            self.guard(connection, lease, lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == lease.job_id)
                .values(heartbeat=now, lease_until=now + timedelta(milliseconds=lease.lease_ms))
            )

    def finish(self, lease: Lease, status: str) -> None:
        if status not in {"paused", "waiting_approval", "outcome_unknown", *TERMINAL_RUNS}:
            raise AgentGitError(ErrorCode.VALIDATION, "Job 终止状态非法")
        with self.history.engine.begin() as connection:
            self.guard(connection, lease, lease.run_id)
            run = self.history._run(connection, lease.repository_id, lease.run_id)
            if run["status"] != status:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "Job 与 Run 状态不一致")
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == lease.job_id)
                .values(
                    status=status,
                    worker=None,
                    lease_until=None,
                    last_error=run["failure_code"],
                    checkpoint_commit_id=run["checkpoint_commit_id"],
                    available_at=sa.func.clock_timestamp() + sa.text("interval '30 seconds'")
                    if status == "outcome_unknown"
                    else sa.func.clock_timestamp(),
                )
            )

    def request_control(
        self,
        repository_id: UUID,
        run_id: UUID,
        action: str,
        *,
        expected_version: int,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        if action not in ("pause", "resume", "cancel"):
            raise AgentGitError(ErrorCode.VALIDATION, "未知控制操作")
        event = EventInput("control.requested", "control", {"action": action})
        obj = self.history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
        )
        runs, jobs = TABLES["runs"], TABLES["jobs"]
        with transaction(self.history.engine, connection) as connection:
            run = self.history._run(connection, repository_id, run_id, lock=True)
            if (
                run["state_version"] != expected_version
                or run["status"] in TERMINAL_RUNS
                or run["control_status"] == "requested"
            ):
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "Run 版本、状态或已有控制请求不匹配"
                )
            if action == "resume" and run["status"] != "paused":
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "普通恢复只允许 paused Run")
            if action == "pause" and run["status"] not in ("queued", "planning", "running"):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "当前 Run 不能请求暂停")
            job = (
                connection.execute(
                    sa.select(jobs)
                    .where(jobs.c.run_id == run_id, jobs.c.repository_id == repository_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if job is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "Run 没有持久化 Job")
            connection.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(control_request=action, control_status="requested")
            )
            seq = int(run["last_event_seq"]) + 1
            self.history._event(connection, repository_id, run_id, seq, event, obj)
            connection.execute(runs.update().where(runs.c.id == run_id).values(last_event_seq=seq))
            if action == "resume" or (
                action == "cancel" and job["status"] in ("paused", "waiting_approval")
            ):
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job["id"])
                    .values(
                        status="queued",
                        available_at=sa.func.clock_timestamp(),
                        worker=None,
                        lease_until=None,
                    )
                )
            result = dict(self.history._run(connection, repository_id, run_id))
        return result

    def apply_control(self, lease: Lease) -> str | None:
        with self.history.engine.begin() as connection:
            row = self.guard(connection, lease, lease.run_id)
            run = self.history._run(connection, lease.repository_id, lease.run_id)
            if run["control_status"] == "requested":
                return str(run["control_request"])
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            if row["deadline_at"] is not None and row["deadline_at"] <= now:
                return "budget_exhausted"
        return None

    def mark_control_applied(self, connection: Connection, lease: Lease) -> None:
        self.guard(connection, lease, lease.run_id)
        runs = TABLES["runs"]
        connection.execute(
            runs.update()
            .where(runs.c.id == lease.run_id, runs.c.control_status == "requested")
            .values(control_status="applied")
        )
