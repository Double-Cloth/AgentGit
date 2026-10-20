"""本机管理用例：幂等动作、项目边界与有界查询供两个入口复用。"""

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.management import (
    ControlRun,
    DecideApproval,
    NewAgent,
    NewRepository,
    NewTask,
    SubmitRun,
)
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, BudgetUsage
from agentgit.services.approvals import ApprovalService
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.database import make_engine
from agentgit.storage.objects import ObjectStore


def public(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): public(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [public(item) for item in value]
    return value


class ManagementService:
    def __init__(self, research: ResearchService, *, actor: str = "local-operator"):
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.CONFIGURATION, "本机操作者身份无效")
        self.research, self.history, self.actor = research, research.history, actor
        self.jobs = JobService(research)

    @classmethod
    def from_settings(cls, settings: Settings, *, testing: bool = False) -> "ManagementService":
        if testing:
            settings.assert_test_isolation()
        root = settings.test_data_root if testing else settings.data_root
        history = HistoryService(
            make_engine(settings, testing=testing),
            ObjectStore(root / "objects", max_bytes=settings.max_object_bytes),
        )
        return cls(ResearchService(history, root), actor=settings.operator_id)

    def _mutate(
        self,
        route: str,
        key: str,
        repository_id: UUID | None,
        payload: dict[str, Any],
        action: Callable[[Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        if not key or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "需要有界、非空的幂等键")
        digest = object_digest(
            ObjectType.BLOB,
            encode(
                {"repository_id": str(repository_id) if repository_id else None, "body": payload}
            ),
        )
        table = TABLES["idempotency_keys"]
        with self.history.engine.begin() as connection:
            scope = encode([self.actor, route, key]).decode()
            connection.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(scope, 0)))
            )
            cached = (
                connection.execute(
                    sa.select(table).where(
                        table.c.actor == self.actor, table.c.route == route, table.c.key == key
                    )
                )
                .mappings()
                .first()
            )
            if cached is not None:
                if cached["request_digest"] != digest or (
                    repository_id is not None and cached["repository_id"] != repository_id
                ):
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一幂等键不能用于不同请求")
                saved = self.history.objects.get_json(cached["result_ref"], ObjectType.BLOB)
                if not isinstance(saved, dict):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "幂等响应格式无效")
                return saved
            result: dict[str, Any] = public(action(connection))
            owner = repository_id or UUID(result["repository_id"])
            response = self.history.objects.put(ObjectType.BLOB, encode(result))
            self.history._ref(connection, owner, response, "request", str(uuid4()), "response")
            connection.execute(
                table.insert().values(
                    actor=self.actor,
                    route=route,
                    key=key,
                    repository_id=owner,
                    request_digest=digest,
                    result_ref=response.id,
                )
            )
            return result

    def create_repository(self, payload: NewRepository, key: str) -> dict[str, Any]:
        return self._mutate(
            "POST /repositories",
            key,
            None,
            payload.model_dump(mode="json"),
            lambda connection: {
                "repository_id": self.history.create_repository(
                    payload.name, connection=connection
                ),
                "version": 0,
            },
        )

    def create_agent(self, repository_id: UUID, payload: NewAgent, key: str) -> dict[str, Any]:
        def action(connection: Connection) -> dict[str, Any]:
            config_id = self.research.create_config(
                repository_id,
                payload.config,
                connection=connection,
                expected_version=payload.expected_version,
            )
            return {
                "repository_id": repository_id,
                "config_id": config_id,
                "version": payload.expected_version + 1,
            }

        return self._mutate(
            "POST /agents", key, repository_id, payload.model_dump(mode="json"), action
        )

    def create_task(self, repository_id: UUID, payload: NewTask, key: str) -> dict[str, Any]:
        def action(connection: Connection) -> dict[str, Any]:
            configs = TABLES["agent_configs"]
            version = connection.execute(
                sa.select(configs.c.version).where(
                    configs.c.repository_id == repository_id,
                    configs.c.id == UUID(payload.config_id),
                )
            ).scalar_one_or_none()
            if version is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "配置不可访问")
            if version != payload.config_version:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "配置版本已改变")
            task = self.research.create_task(
                repository_id, UUID(payload.config_id), payload.goal, connection=connection
            )
            return {
                "repository_id": repository_id,
                "task_id": task,
                "config_id": payload.config_id,
                "version": 0,
            }

        return self._mutate(
            "POST /tasks", key, repository_id, payload.model_dump(mode="json"), action
        )

    def submit(
        self, repository_id: UUID, task_id: UUID, payload: SubmitRun, key: str
    ) -> dict[str, Any]:
        return self.jobs.enqueue(
            repository_id,
            task_id,
            actor=self.actor,
            key=key,
            **payload.model_dump(exclude={"schema_version", "budget"}),
            budget=payload.budget,
        ).model_dump(mode="json")

    def control(
        self, repository_id: UUID, run_id: UUID, action: str, payload: ControlRun, key: str
    ) -> dict[str, Any]:
        return self._mutate(
            f"POST /runs/{run_id}/{action}",
            key,
            repository_id,
            payload.model_dump(mode="json"),
            lambda connection: {
                **self.jobs.request_control(
                    repository_id,
                    run_id,
                    action,
                    expected_version=payload.expected_version,
                    connection=connection,
                ),
                "creates_new_run": False,
            },
        )

    def decide(
        self, repository_id: UUID, approval_id: UUID, payload: DecideApproval, key: str
    ) -> dict[str, Any]:
        def action(connection: Connection) -> dict[str, Any]:
            table = TABLES["approvals"]
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository_id, table.c.id == approval_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "审批不可访问")
            policy = parse_contract(
                self.history.objects.get(row["policy_digest"], ObjectType.BLOB), ToolPolicy
            )
            service = ApprovalService(self.history, policy, actor=self.actor)
            return service.decide(
                repository_id,
                approval_id,
                actor=self.actor,
                decision=payload.decision,
                expected_version=payload.expected_version,
                connection=connection,
            )

        return self._mutate(
            f"POST /approvals/{approval_id}/decision",
            key,
            repository_id,
            payload.model_dump(mode="json"),
            action,
        )

    def run(self, repository_id: UUID, run_id: UUID) -> dict[str, Any]:
        row = self.jobs.run(repository_id, run_id)
        result: dict[str, Any] = public(row)
        branch = self.history.branch(repository_id, row["branch_name"])
        result.update(branch_head=branch["head_commit_id"], branch_version=branch["version"])
        if row["agent_config_id"] is None:
            result["budget"] = None
            return result
        config = self.research.config(repository_id, row["agent_config_id"])
        usage = BudgetUsage()
        with self.history.engine.connect() as connection:
            snapshots, jobs = TABLES["snapshots"], TABLES["jobs"]
            snapshot = connection.execute(
                sa.select(snapshots.c.run_id).where(
                    snapshots.c.repository_id == repository_id,
                    snapshots.c.commit_id == row["checkpoint_commit_id"],
                )
            ).first()
            job = (
                connection.execute(sa.select(jobs).where(jobs.c.run_id == run_id))
                .mappings()
                .first()
            )
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            allowed = {run_id}
            if job is not None and job["origin_run_id"] is not None:
                origin = row["origin_run_id"]
                while origin is not None:
                    if origin in allowed or len(allowed) >= 100:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "预算来源链非法")
                    allowed.add(origin)
                    if origin == job["origin_run_id"]:
                        break
                    origin = self.history._run(connection, repository_id, origin)["origin_run_id"]
                if job["origin_run_id"] not in allowed:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "预算来源链未到达 Job 根")
        if snapshot is not None and snapshot.run_id in allowed:
            restored = self.history.restore(repository_id, row["checkpoint_commit_id"])
            usage = parse_contract(encode(restored.data), AgentState).usage
        limits = self.jobs.payload(job).limits if job is not None else None
        limits = limits or config.limits
        wall = (
            limits.max_wall_ms
            if job is None or job["deadline_at"] is None
            else max(0, int((job["deadline_at"] - now).total_seconds() * 1000))
        )
        result["budget"] = {
            "limits": limits.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
            "remaining": {
                "steps": max(0, limits.max_steps - usage.steps),
                "model_calls": max(0, limits.max_model_calls - usage.model_calls),
                "tool_calls": max(0, limits.max_tool_calls - usage.tool_calls),
                "tokens": max(0, limits.max_tokens - usage.tokens - usage.reserved_tokens),
                "wall_ms": wall,
            },
        }
        return result

    def job(self, repository_id: UUID, job_id: UUID) -> dict[str, Any]:
        row = self.jobs.get(repository_id, job_id)
        result: dict[str, Any] = public(
            {
                key: row[key]
                for key in (
                    "id",
                    "repository_id",
                    "run_id",
                    "kind",
                    "status",
                    "attempt",
                    "checkpoint_commit_id",
                    "last_error",
                    "created_at",
                    "started_at",
                    "deadline_at",
                )
            }
        )
        run = self.run(repository_id, row["run_id"])
        result.update(
            {
                key: run[key]
                for key in ("control_request", "control_status", "state_version", "budget")
            }
        )
        return result

    def events(
        self, repository_id: UUID, run_id: UUID, after_seq: int, limit: int
    ) -> dict[str, Any]:
        rows = self.history.events(repository_id, run_id, after=after_seq, limit=limit)
        items = []
        for row in rows:
            item = public(row)
            item["payload"] = self.history.objects.get_json(row["payload_ref"], ObjectType.EVENT)[
                "data"
            ]
            items.append(item)
        return {"items": items, "next_after_seq": rows[-1]["seq"] if rows else after_seq}

    def artifacts(self, repository_id: UUID, run_id: UUID) -> dict[str, Any]:
        row = self.jobs.run(repository_id, run_id)
        return {
            "items": public(self.history.artifacts(repository_id, row["checkpoint_commit_id"]))
            if row["checkpoint_commit_id"]
            else []
        }

    def artifact(self, repository_id: UUID, artifact_id: UUID) -> tuple[dict[str, Any], bytes]:
        table = TABLES["artifacts"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository_id, table.c.id == artifact_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "产物不可访问")
        return public(dict(row)), self.history.read_artifact(repository_id, artifact_id)

    def repository(self, repository_id: UUID) -> dict[str, Any]:
        table = TABLES["repositories"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(table.c.id == repository_id))
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        result: dict[str, Any] = public(dict(row))
        result["branch"] = public(self.history.branch(repository_id))
        return result

    def metrics(self, repository_id: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            repo, jobs = TABLES["repositories"], TABLES["jobs"]
            if (
                connection.execute(sa.select(repo.c.id).where(repo.c.id == repository_id)).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
            rows = connection.execute(
                sa.select(jobs.c.status, sa.func.count())
                .where(jobs.c.repository_id == repository_id)
                .group_by(jobs.c.status)
            )
            return {"jobs": {row[0]: row[1] for row in rows}}

    def approvals(
        self, repository_id: UUID, run_id: UUID, after_seq: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        if after_seq < 0 or not 1 <= limit <= 1000:
            raise AgentGitError(ErrorCode.VALIDATION, "审批分页参数越界")
        table = TABLES["approvals"]
        with self.history.engine.connect() as connection:
            self.history._run(connection, repository_id, run_id)
            numbered = (
                sa.select(
                    table,
                    sa.func.row_number()
                    .over(order_by=(table.c.created_at, table.c.id))
                    .label("sequence"),
                )
                .where(table.c.repository_id == repository_id, table.c.run_id == run_id)
                .subquery()
            )
            rows = list(
                connection.execute(
                    sa.select(numbered)
                    .where(numbered.c.sequence > after_seq)
                    .order_by(numbered.c.sequence)
                    .limit(limit)
                ).mappings()
            )
        items = []
        for row in rows:
            item = public(dict(row))
            item["action"] = self.history.objects.get_json(row["action_ref"], ObjectType.CALL)
            action = item["action"]
            item["request"] = self.history.objects.get_json(
                action["occurrence"]["request_digest"], ObjectType.BLOB
            )
            items.append(item)
        return {"items": items, "next_after_seq": rows[-1]["sequence"] if rows else after_seq}
