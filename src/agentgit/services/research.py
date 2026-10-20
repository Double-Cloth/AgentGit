"""配置、任务、离线执行与 S2 原子提交的用例编排。"""

import hashlib
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, ResearchGoal
from agentgit.runtime.engine import ResearchRuntime
from agentgit.runtime.ports import ModelAdapter
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.history import ArtifactInput, EventInput, HistoryService
from agentgit.storage.schema_s3 import build_metadata

TABLES = build_metadata().tables


class ResearchService:
    def __init__(self, history: HistoryService, data_root: Path, *, corpus: Corpus | None = None):
        self.history, self.data_root = history, data_root.resolve()
        self.corpus = corpus or Corpus.bundled()

    def create_config(self, repository_id: UUID, config: AgentConfig | None = None) -> UUID:
        config = config or AgentConfig()
        obj = self.history.objects.put(ObjectType.BLOB, encode(config.model_dump(mode="json")))
        config_id = uuid4()
        with self.history.engine.begin() as connection:
            repo = TABLES["repositories"]
            if (
                connection.execute(
                    sa.select(repo.c.id).where(repo.c.id == repository_id).with_for_update()
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
            table = TABLES["agent_configs"]
            version = (
                connection.execute(
                    sa.select(sa.func.coalesce(sa.func.max(table.c.version), 0)).where(
                        table.c.repository_id == repository_id
                    )
                ).scalar_one()
                + 1
            )
            self.history._ref(
                connection, repository_id, obj, "agent_config", str(config_id), "config"
            )
            connection.execute(
                table.insert().values(
                    id=config_id, repository_id=repository_id, version=version, config_ref=obj.id
                )
            )
        return config_id

    def config(self, repository_id: UUID, config_id: UUID) -> AgentConfig:
        table = TABLES["agent_configs"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository_id, table.c.id == config_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "配置不可访问")
        return parse_contract(
            self.history.objects.get(row["config_ref"], ObjectType.BLOB), AgentConfig
        )

    def create_task(self, repository_id: UUID, config_id: UUID, goal: ResearchGoal) -> UUID:
        config = self.config(repository_id, config_id)
        if set(goal.frameworks) != {source.framework for source in self.corpus.sources.values()}:
            raise AgentGitError(ErrorCode.VALIDATION, "S3 仅支持固定三框架比较任务")
        task_id = uuid4()
        documents = {
            "goal_ref": goal.model_dump(mode="json"),
            "budget_ref": config.limits.model_dump(mode="json"),
            "policy_ref": {
                "schema_version": 1,
                "tools": list(config.tools),
                "scope": "local_run_only",
            },
        }
        objects = {
            role: self.history.objects.put(ObjectType.BLOB, encode(value))
            for role, value in documents.items()
        }
        with self.history.engine.begin() as connection:
            for role, obj in objects.items():
                self.history._ref(connection, repository_id, obj, "task", str(task_id), role)
            connection.execute(
                TABLES["tasks"]
                .insert()
                .values(
                    id=task_id,
                    repository_id=repository_id,
                    agent_config_id=config_id,
                    **{role: obj.id for role, obj in objects.items()},
                )
            )
        return task_id

    def start(
        self, repository_id: UUID, task_id: UUID, *, branch_name: str = "main"
    ) -> "ResearchSession":
        task_table, runs, branches = TABLES["tasks"], TABLES["runs"], TABLES["branches"]
        run_id = uuid4()
        with self.history.engine.begin() as connection:
            task = (
                connection.execute(
                    sa.select(task_table)
                    .where(task_table.c.repository_id == repository_id, task_table.c.id == task_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if task is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "任务不可访问")
            if task["active_run_id"] is not None:
                status = connection.execute(
                    sa.select(runs.c.status).where(runs.c.id == task["active_run_id"])
                ).scalar_one()
                if status not in ("completed", "failed", "cancelled", "budget_exhausted"):
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "任务已有活动 Run")
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
            connection.execute(
                runs.insert().values(
                    id=run_id,
                    repository_id=repository_id,
                    task_id=task_id,
                    agent_config_id=task["agent_config_id"],
                    branch_name=branch_name,
                    base_commit_id=branch["head_commit_id"],
                    checkpoint_commit_id=branch["head_commit_id"],
                    origin_run_id=task["active_run_id"],
                    mode="live",
                    status="queued",
                )
            )
            connection.execute(
                task_table.update().where(task_table.c.id == task_id).values(active_run_id=run_id)
            )
        config = self.config(repository_id, task["agent_config_id"])
        goal = parse_contract(
            self.history.objects.get(task["goal_ref"], ObjectType.BLOB), ResearchGoal
        )
        workspace = ArtifactWorkspace(
            self.data_root / "workspaces" / str(run_id),
            max_file_bytes=config.limits.max_artifact_bytes,
            max_files=config.limits.max_files,
        )
        nodes = TABLES["task_nodes"]
        with self.history.engine.connect() as connection:
            plan_version = connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(nodes.c.plan_version), 0)).where(
                    nodes.c.task_id == task_id
                )
            ).scalar_one()
        return ResearchSession(
            self,
            repository_id,
            task_id,
            run_id,
            config,
            AgentState(goal=goal, plan_version=plan_version),
            workspace,
            branch["head_commit_id"],
            int(branch["version"]),
        )


class ResearchSession:
    def __init__(
        self,
        service: ResearchService,
        repository_id: UUID,
        task_id: UUID,
        run_id: UUID,
        config: AgentConfig,
        state: AgentState,
        workspace: ArtifactWorkspace,
        head: str | None,
        version: int,
    ):
        self.service, self.history = service, service.history
        self.repository_id, self.task_id, self.run_id = repository_id, task_id, run_id
        self.config, self.state, self.workspace = config, state, workspace
        self.head, self.version = head, version
        self.tools = ToolRegistry(service.corpus, workspace, config)
        self.environment: EnvironmentManifest | None = None

    def _environment(self, model: ModelAdapter) -> EnvironmentManifest:
        package = Path(__file__).resolve().parents[1]
        lock = package.parent.parent / "uv.lock"
        if not lock.is_file():
            raise AgentGitError(ErrorCode.CONFIGURATION, "S3 示例需从含 uv.lock 的项目环境运行")
        source_hash = hashlib.sha256()
        for path in sorted(package.rglob("*.py")):
            source_hash.update(
                path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes()
            )
        payload = encode(
            {
                "schema_version": 1,
                "goal": self.state.goal.model_dump(mode="json"),
                "corpus_digest": self.service.corpus.digest(),
                "config": self.config.model_dump(mode="json"),
            }
        )
        input_ref = self.history.register_input(self.repository_id, payload)
        return EnvironmentManifest(
            runtime_version="0.1.0",
            python_version="3.12",
            code_revision="source-sha256:" + source_hash.hexdigest(),
            dependency_lock_digest=hashlib.sha256(lock.read_bytes()).hexdigest(),
            model_signature="mock-research/v1:" + model.signature,
            prompt_digest=hashlib.sha256(self.config.prompt.encode()).hexdigest(),
            tool_signatures=tuple(self.tools.signature(name) for name in sorted(self.config.tools)),
            input_ref=input_ref,
            evaluation_time=self.service.corpus.evaluation_time,
            random_seed=0,
        )

    def execute(
        self, model: ModelAdapter | None = None, *, clock_ms: Callable[[], int] | None = None
    ) -> AgentState:
        model = model or MockResearchModel()
        self.environment = self._environment(model)
        gateway = RecordedCallGateway(
            self.history, self.repository_id, self.run_id, lambda: (self.head, self.version)
        )
        return ResearchRuntime(
            self.state, self.config, model, self.tools, gateway, self, clock_ms=clock_ms
        ).run()

    def save(self, state: AgentState, event_type: str, details: dict[str, object]) -> None:
        if self.environment is None:
            raise AgentGitError(ErrorCode.CONFIGURATION, "提交前缺少环境清单")
        contents = self.workspace.contents()
        artifacts = tuple(
            ArtifactInput(name, value, self.workspace.media_types[name])
            for name, value in contents.items()
        )
        plan_obj = (
            self.history.objects.put(ObjectType.BLOB, encode(state.plan.model_dump(mode="json")))
            if state.plan
            else None
        )
        dependencies = (
            {
                node.key: self.history.objects.put(
                    ObjectType.BLOB,
                    encode({"schema_version": 1, "dependencies": list(node.dependencies)}),
                )
                for node in state.plan.nodes
            }
            if state.plan
            else {}
        )

        def projection(connection: Connection) -> None:
            task = TABLES["tasks"]
            active = connection.execute(
                sa.select(task.c.active_run_id)
                .where(task.c.repository_id == self.repository_id, task.c.id == self.task_id)
                .with_for_update()
            ).scalar_one()
            if active != self.run_id:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "任务活动 Run 已改变")
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.run_id)
                .values(status=state.status.value)
            )
            if state.plan is None or plan_obj is None:
                return
            self.history._ref(
                connection,
                self.repository_id,
                plan_obj,
                "task",
                str(self.task_id),
                f"plan:{state.plan_version}",
            )
            table = TABLES["task_nodes"]
            for node in state.plan.nodes:
                deps = dependencies[node.key]
                self.history._ref(
                    connection,
                    self.repository_id,
                    deps,
                    "task",
                    str(self.task_id),
                    f"deps:{state.plan_version}:{node.key}",
                )
                where = (
                    table.c.task_id == self.task_id,
                    table.c.plan_version == state.plan_version,
                    table.c.node_key == node.key,
                )
                connection.execute(
                    insert(table)
                    .values(
                        repository_id=self.repository_id,
                        task_id=self.task_id,
                        plan_version=state.plan_version,
                        node_key=node.key,
                        plan_ref=plan_obj.id,
                        deps_ref=deps.id,
                        status="pending",
                    )
                    .on_conflict_do_nothing()
                )
                existing = connection.execute(
                    sa.select(table.c.plan_ref).where(*where)
                ).scalar_one()
                if existing != plan_obj.id:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "不能原地改变历史计划定义")
                result = next(
                    (
                        item.response_ref
                        for item in reversed(state.messages)
                        if item.logical_step_id == node.key and item.error_code is None
                    ),
                    None,
                )
                connection.execute(
                    table.update()
                    .where(*where)
                    .values(
                        status="completed" if node.key in state.completed else "pending",
                        result_ref=result if node.key in state.completed else None,
                    )
                )

        result = self.history.commit(
            self.repository_id,
            self.run_id,
            expected_head=self.head,
            expected_version=self.version,
            state=state.model_dump(mode="json"),
            environment=self.environment,
            events=(EventInput(event_type, f"state-{state.usage.steps}", details),),
            artifacts=artifacts,
            projection=projection,
        )
        self.head, self.version, self.state = result.commit_id, result.branch_version, state
