"""配置、任务、离线执行与 S2 原子提交的用例编排。"""

import hashlib
import platform
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.adapters.corpus import Corpus
from agentgit.adapters.execution.policy import bundled_policy
from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import ExecutionContext, ExecutionManifest, ExecutionResult
from agentgit.core.models import EffectClass, EnvironmentManifest, parse_contract
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    CallTrace,
    ResearchGoal,
    SavedCallOutcome,
)
from agentgit.runtime.engine import ResearchRuntime
from agentgit.runtime.ports import CallSettlement, ModelAdapter
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import source_revision
from agentgit.services.history import ArtifactInput, EventInput, HistoryService
from agentgit.storage.database import transaction
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables

if TYPE_CHECKING:
    from agentgit.services.executions import ExecutionService


class ResearchService:
    def __init__(self, history: HistoryService, data_root: Path, *, corpus: Corpus | None = None):
        self.history, self.data_root = history, data_root.resolve()
        self.corpus = corpus or Corpus.bundled()

    def create_config(
        self,
        repository_id: UUID,
        config: AgentConfig | None = None,
        *,
        connection: Connection | None = None,
        expected_version: int | None = None,
    ) -> UUID:
        config = config or AgentConfig()
        if config.execution_policy is not None and config.execution_policy != bundled_policy():
            raise AgentGitError(ErrorCode.FORBIDDEN, "新配置只能绑定后端当前已审阅的固定执行策略")
        obj = self.history.objects.put(ObjectType.BLOB, encode(config.model_dump(mode="json")))
        config_id = uuid4()
        with transaction(self.history.engine, connection) as connection:
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
            if expected_version is not None and version != expected_version + 1:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "配置版本已改变")
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

    def create_task(
        self,
        repository_id: UUID,
        config_id: UUID,
        goal: ResearchGoal,
        *,
        connection: Connection | None = None,
    ) -> UUID:
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
        with transaction(self.history.engine, connection) as connection:
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
            config = self.config(repository_id, task["agent_config_id"])
            if config.execution_policy is not None:
                raise AgentGitError(
                    ErrorCode.FORBIDDEN, "容器工具任务必须提交持久 Job，由独立 Worker 执行"
                )
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
            if branch["active_writer_run_id"] is not None:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "分支已有活动写入 Run")
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
        self.control_applied: Callable[[Connection], None] | None = None
        self.gateway_factory: Callable[[], RecordedCallGateway] | None = None
        self.expected_environment: EnvironmentManifest | None = None
        self.expected_code_revision: str | None = None
        self.after_checkpoint: Callable[[], None] | None = None
        self.authorization: ApprovalService | None = None
        self.settlement: CallSettlement | None = None
        self.execution_service: ExecutionService | None = None

    def bind_execution(self, service: "ExecutionService") -> None:
        if service.policy != self.config.execution_policy:
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行服务与冻结配置的策略不符")
        self.execution_service = service
        self.tools.input_context = self._execution_context
        self.tools.execution_files = self._execution_files

    def _execution_context(self) -> ExecutionContext:
        if self.execution_service is None or self.head is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "容器工具必须具有持久 Job 和安全检查点")
        manifest_ref, _ = self.execution_service.authorize_inputs(
            self.repository_id,
            self.run_id,
            self.head,
            self.workspace.contents(),
            media_types=self.workspace.media_types,
        )
        manifest = parse_contract(
            self.history.objects.get(manifest_ref, ObjectType.ARTIFACT_MANIFEST), ExecutionManifest
        )
        return ExecutionContext(
            input_manifest_ref=manifest_ref,
            execution_policy_ref=self.execution_service.policy.digest(),
            files=manifest.files,
        )

    def _execution_files(
        self, result: ExecutionResult, trace: CallTrace
    ) -> tuple[dict[str, bytes], dict[str, str]]:
        calls = TABLES["external_calls"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == self.repository_id,
                        calls.c.run_id == self.run_id,
                        calls.c.logical_step_id == trace.logical_step_id,
                        calls.c.call_index == trace.call_index,
                        calls.c.attempt == trace.attempt,
                        calls.c.provider == trace.provider,
                        calls.c.signature_digest == trace.signature_digest,
                        calls.c.request_digest == trace.request_ref,
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["result_ref"] is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "执行观察缺少当前 Run 的录制来源")
        saved = parse_contract(
            self.history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
        )
        if (
            not saved.complete
            or saved.error is not None
            or saved.response_ref != trace.response_ref
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "执行观察与完整录制结果不符")
        assert result.output_manifest_ref is not None
        manifest = parse_contract(
            self.history.objects.get(result.output_manifest_ref, ObjectType.ARTIFACT_MANIFEST),
            ExecutionManifest,
        )
        files: dict[str, bytes] = {}
        media: dict[str, str] = {}
        for item in manifest.files:
            payload = self.history.objects.get(item.object_ref, ObjectType.BLOB)
            if len(payload) != item.byte_length:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "输出文件长度与录制清单不符")
            files[item.path], media[item.path] = payload, item.media_type
        return files, media

    def _environment(self, model: ModelAdapter) -> EnvironmentManifest:
        package = Path(__file__).resolve().parents[1]
        lock = package.parent.parent / "uv.lock"
        if not lock.is_file():
            raise AgentGitError(ErrorCode.CONFIGURATION, "S3 示例需从含 uv.lock 的项目环境运行")
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
            runtime_version=__version__,
            python_version=platform.python_version(),
            code_revision=source_revision(),
            dependency_lock_digest=hashlib.sha256(lock.read_bytes()).hexdigest(),
            model_signature="mock-research/v1:" + model.signature,
            prompt_digest=hashlib.sha256(self.config.prompt.encode()).hexdigest(),
            tool_signatures=tuple(self.tools.signature(name) for name in sorted(self.config.tools)),
            input_ref=input_ref,
            evaluation_time=self.service.corpus.evaluation_time,
            random_seed=0,
        )

    def execute(
        self,
        model: ModelAdapter | None = None,
        *,
        clock_ms: Callable[[], int] | None = None,
        control: Callable[[], str | None] | None = None,
        wall_budget_ms: int | None = None,
    ) -> AgentState:
        model = model or MockResearchModel()
        if self.config.execution_policy is not None and self.execution_service is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "容器工具必须通过持久 Job/Worker 执行")
        self.environment = self._environment(model)
        if (
            self.expected_code_revision is not None
            and self.environment.code_revision != self.expected_code_revision
        ):
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "排队时的代码版本与当前 Worker 不一致"
            )
        if self.expected_environment is not None and self.environment != self.expected_environment:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "恢复环境与原检查点不兼容")
        gateway = (
            self.gateway_factory()
            if self.gateway_factory is not None
            else RecordedCallGateway(
                self.history, self.repository_id, self.run_id, lambda: (self.head, self.version)
            )
        )
        if gateway.authorization is None and self.authorization is not None:
            gateway.authorization = self.authorization
        if self.execution_service is not None:
            gateway.managed = self.execution_service
        if gateway.authorization is None:
            rules = [
                ToolRule(
                    provider="llm:mock-research/v1",
                    signature_digest=model.signature,
                    effect=EffectClass.READ_ONLY,
                    scope="local",
                    preauthorized=True,
                )
            ]
            rules.extend(
                ToolRule(
                    provider=f"tool:{name}/v1",
                    signature_digest=self.tools.signature(name),
                    effect=self.tools.effect(name),
                    scope="sandbox" if name in {"terminal_exec", "code_exec"} else "local",
                    preauthorized=True,
                )
                for name in sorted(self.config.tools)
            )
            gateway.authorization = ApprovalService(
                self.history, ToolPolicy(rules=tuple(rules)), actor="local-operator"
            )
        return ResearchRuntime(
            self.state,
            self.config,
            model,
            self.tools,
            gateway,
            self,
            clock_ms=clock_ms,
            control=control,
            wall_budget_ms=wall_budget_ms,
            settlement=self.settlement,
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
                .values(status=state.status.value, failure_code=state.error_code)
            )
            if self.control_applied is not None and event_type in (
                "run.paused",
                "run.resumed",
                "run.cancelled",
            ):
                self.control_applied(connection)
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
        if self.after_checkpoint is not None:
            self.after_checkpoint()
