"""桌面只读列表；游标必须属于当前项目及所选版本。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import ResearchGoal
from agentgit.services.history import TABLES, HistoryService


class InspectionService:
    def __init__(self, history: HistoryService):
        self.history = history

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentGitError(ErrorCode.VALIDATION, "列表分页大小必须为 1 至 100")

    def runs(
        self, repository: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        self._limit(limit)
        self.history.branch(repository)
        runs, jobs, tasks = TABLES["runs"], TABLES["jobs"], TABLES["tasks"]
        with self.history.engine.connect() as connection:
            statement = (
                sa.select(
                    runs,
                    jobs.c.id.label("job_id"),
                    jobs.c.status.label("job_status"),
                    tasks.c.goal_ref,
                )
                .outerjoin(
                    jobs, sa.and_(jobs.c.repository_id == repository, jobs.c.run_id == runs.c.id)
                )
                .outerjoin(
                    tasks,
                    sa.and_(tasks.c.repository_id == repository, tasks.c.id == runs.c.task_id),
                )
                .where(runs.c.repository_id == repository)
            )
            if after_id is not None:
                anchor = connection.execute(
                    sa.select(runs.c.created_at, runs.c.id).where(
                        runs.c.repository_id == repository, runs.c.id == after_id
                    )
                ).first()
                if anchor is None:
                    raise AgentGitError(ErrorCode.NOT_FOUND, "运行游标不可访问")
                statement = statement.where(
                    sa.tuple_(runs.c.created_at, runs.c.id)
                    < sa.tuple_(anchor.created_at, anchor.id)
                )
            rows = list(
                connection.execute(
                    statement.order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(limit + 1)
                ).mappings()
            )
        items = []
        for row in rows[:limit]:
            item = {
                key: row[key]
                for key in (
                    "id",
                    "repository_id",
                    "task_id",
                    "agent_config_id",
                    "branch_name",
                    "mode",
                    "status",
                    "origin_run_id",
                    "base_commit_id",
                    "checkpoint_commit_id",
                    "state_version",
                    "control_request",
                    "control_status",
                    "failure_code",
                    "created_at",
                    "job_id",
                    "job_status",
                )
            }
            item["goal"] = None
            if row["goal_ref"] is not None:
                item["goal"] = parse_contract(
                    self.history.objects.get(row["goal_ref"], ObjectType.BLOB), ResearchGoal
                ).model_dump(mode="json")
            items.append(item)
        return {
            "items": items,
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def artifacts(
        self, repository: UUID, commit: str, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        self._limit(limit)
        table = TABLES["artifacts"]
        with self.history.engine.connect() as connection:
            self.history._commit_row(connection, repository, commit)
            statement = sa.select(table).where(
                table.c.repository_id == repository, table.c.commit_id == commit
            )
            if after_id is not None:
                anchor = connection.execute(
                    sa.select(table.c.relative_path).where(
                        table.c.repository_id == repository,
                        table.c.commit_id == commit,
                        table.c.id == after_id,
                    )
                ).scalar_one_or_none()
                if anchor is None:
                    raise AgentGitError(ErrorCode.NOT_FOUND, "产物游标不属于所选检查点")
                statement = statement.where(table.c.relative_path > anchor)
            rows = list(
                connection.execute(
                    statement.order_by(table.c.relative_path).limit(limit + 1)
                ).mappings()
            )
        return {
            "items": [dict(row) for row in rows[:limit]],
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            "commit_id": commit,
        }
