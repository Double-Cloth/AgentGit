"""本机管理用例：幂等动作、项目边界与有界查询供两个入口复用。"""

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.adapters.execution.policy import bundled_policy
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FreezeValidator, NewExperiment
from agentgit.core.interventions import NewIntervention
from agentgit.core.management import (
    ControlRun,
    DecideApproval,
    NewAgent,
    NewRepository,
    NewTask,
    SubmitRun,
)
from agentgit.core.model_usage import OpenAIOptions, price_micro_usd
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage
from agentgit.core.versions import LiveReplay, NewBranch, RecordedRange, RecordedReplay
from agentgit.runtime.model_budget import call_budget
from agentgit.services.approvals import ApprovalService
from agentgit.services.debug_experiments import DebugExperimentService
from agentgit.services.debug_reports import DebugReportService
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.execution_views import ExecutionResource, ExecutionViewService
from agentgit.services.experiments import ExperimentService
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.inspection import InspectionService
from agentgit.services.jobs import JobService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.repair_templates import prepare_templates
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
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
        self.versions = VersionService(self.history)
        self.replays = ReplayService(research)
        self.inspection = InspectionService(self.history)
        self.behavior = BehaviorService(self.history)
        self.debug_reports = DebugReportService(self.history)
        self.execution_views = ExecutionViewService(self.history)
        self.experiments = ExperimentService(research)
        self.debug_experiments = DebugExperimentService(self.debug_reports, self.experiments)

    def create_repair_search(
        self,
        repository_id: UUID,
        payload: NewRepairSearch,
        key: str,
    ) -> dict[str, Any]:
        body = payload.model_dump(mode="json")
        # 原始请求身份先查缓存：进程在搜索提交后、返回前退出也不会重新生成模板。
        self._mutation_digest(key, repository_id, body)
        request = {"repository_id": str(repository_id), "template_request": body}
        digest = object_digest(ObjectType.BLOB, encode(request))
        searches = RepairSearchService(self.history)
        with self.history.engine.connect() as connection:
            cached = searches._cached(connection, self.actor, key, digest)
        if cached is not None:
            return cached
        plan = prepare_templates(self.history, repository_id, payload)
        identities = tuple(
            self.create_intervention(
                repository_id,
                patch.intervention,
                "repair-template:"
                + object_digest(ObjectType.BLOB, encode([self.actor, key, digest, index])),
            )["intervention_id"]
            for index, patch in enumerate(plan.space.patches)
        )
        return searches.submit(
            plan.space,
            plan.budget,
            identities,
            actor=self.actor,
            key=key,
            hybrid=payload.hybrid,
            _request_identity=request,
        )

    def repair_search(self, repository_id: UUID, search_id: UUID) -> dict[str, Any]:
        return RepairJobService(self.history).view(repository_id, search_id)

    def create_repair_study(
        self, repository_id: UUID, payload: NewRepairStudy, key: str
    ) -> dict[str, Any]:
        return RepairStudyService(self.history).submit(
            repository_id, payload, actor=self.actor, key=key
        )

    def repair_study(self, repository_id: UUID, study_id: UUID) -> dict[str, Any]:
        return RepairStudyService(self.history).view(repository_id, study_id)

    def cancel_repair_study(self, repository_id: UUID, study_id: UUID, key: str) -> dict[str, Any]:
        return self._mutate(
            f"POST /repair-studies/{study_id}/cancel",
            key,
            repository_id,
            {"study_id": str(study_id)},
            lambda connection: RepairStudyService(self.history).cancel(
                repository_id, study_id, actor=self.actor, connection=connection
            ),
        )

    def repair_searches(
        self,
        repository_id: UUID,
        *,
        after_id: UUID | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        return RepairJobService(self.history).listing(repository_id, after_id=after_id, limit=limit)

    def cancel_repair_search(
        self,
        repository_id: UUID,
        search_id: UUID,
        key: str,
    ) -> dict[str, Any]:
        return self._mutate(
            f"POST /repair-searches/{search_id}/cancel",
            key,
            repository_id,
            {"search_id": str(search_id)},
            lambda connection: RepairJobService(self.history).cancel(
                repository_id,
                search_id,
                actor=self.actor,
                connection=connection,
            ),
        )

    def create_experiment(
        self, repository_id: UUID, payload: NewExperiment, key: str
    ) -> dict[str, Any]:
        return self._mutate(
            "POST /experiments",
            key,
            repository_id,
            payload.model_dump(mode="json"),
            lambda connection: self.experiments.submit(
                repository_id, payload, actor=self.actor, connection=connection
            ),
        )

    def create_intervention(
        self, repository_id: UUID, payload: NewIntervention, key: str
    ) -> dict[str, Any]:
        route = "POST /interventions"
        body = payload.model_dump(mode="json")
        digest = self._mutation_digest(key, repository_id, body)
        with self.history.engine.connect() as connection:
            cached = self._cached_response(connection, route, key, repository_id, digest)
        if cached is not None:
            return cached
        prepared = self.experiments.interventions.prepare(repository_id, payload, actor=self.actor)
        return self._mutate(
            route,
            key,
            repository_id,
            body,
            lambda connection: self.experiments.interventions.publish(
                prepared, connection=connection
            ),
        )

    def intervention(self, repository_id: UUID, intervention_id: UUID) -> dict[str, Any]:
        definition, payload = self.experiments.interventions.get(repository_id, intervention_id)
        return {
            "intervention_id": str(intervention_id),
            "definition": definition.model_dump(mode="json"),
            "payload": payload.model_dump(mode="json"),
        }

    def intervention_context(
        self,
        repository_id: UUID,
        source_run_id: UUID,
        from_commit: str,
        target_event_id: int,
        validator_ref: str,
    ) -> dict[str, Any]:
        return self.experiments.interventions.context(
            repository_id, source_run_id, from_commit, target_event_id, validator_ref
        )

    def freeze_validator(
        self, repository_id: UUID, payload: FreezeValidator, key: str
    ) -> dict[str, Any]:
        def action(connection: Connection) -> dict[str, Any]:
            self.versions.select_range(
                repository_id,
                RecordedRange(
                    source_run_id=payload.source_run_id,
                    from_commit=payload.from_commit,
                    until_commit=payload.from_commit,
                ),
            )
            view = self.versions.checkout(repository_id, payload.from_commit)
            state = parse_contract(encode(view["snapshot"]["data"]), AgentState)
            corpus = self.replays._source_corpus(repository_id, UUID(payload.source_run_id))
            source_input = self.history.objects.get_json(
                view["environment"]["input_ref"], ObjectType.BLOB
            )
            if (
                not isinstance(source_input, dict)
                or source_input.get("goal") != state.goal.model_dump(mode="json")
                or source_input.get("corpus_digest") != corpus.digest()
                or view["environment"]["evaluation_time"] != corpus.evaluation_time
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "来源冻结输入或语料无法复核")
            ref = self.experiments.validators.freeze(
                repository_id,
                state.goal,
                corpus,
                fixture_split=payload.fixture_split,
                connection=connection,
            )
            return {"schema_version": 1, "repository_id": str(repository_id), "validator_ref": ref}

        return self._mutate(
            "POST /validators", key, repository_id, payload.model_dump(mode="json"), action
        )

    def validator(self, repository_id: UUID, validator_ref: str) -> dict[str, Any]:
        manifest = self.experiments.validators.manifest(repository_id, validator_ref)
        return {"validator_ref": validator_ref, "manifest": manifest.model_dump(mode="json")}

    def experiment(self, repository_id: UUID, experiment_id: UUID) -> dict[str, Any]:
        return self.experiments.report(repository_id, experiment_id)

    def experiments_list(
        self, repository_id: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        result: dict[str, Any] = public(
            self.experiments.listing(repository_id, after_id=after_id, limit=limit)
        )
        return result

    def capabilities(self) -> dict[str, Any]:
        config = AgentConfig()
        provider = OpenAIOptions()
        execution = AgentConfig(
            tools=(*config.tools, "code_exec", "terminal_exec"), execution_policy=bundled_policy()
        )
        return {
            "schema_version": 1,
            "version": __version__,
            "default_config": config.model_dump(mode="json"),
            "execution_config": execution.model_dump(mode="json"),
            "models": [
                {
                    "id": config.model,
                    "title": "Mock · 离线",
                    "external": False,
                    "access": "not_required",
                    "config_patch": {"model": config.model},
                },
                {
                    "id": "openai/" + provider.model,
                    "title": "OpenAI · GPT-5.4 Mini（固定快照）",
                    "external": True,
                    "access": "unknown",
                    "currency": "USD",
                    "config_patch": {
                        "model": "openai/" + provider.model,
                        "openai": provider.model_dump(mode="json"),
                    },
                    "minimum_reservation": {
                        "tokens": provider.max_input_tokens + provider.max_output_tokens,
                        "cost_micro_usd": price_micro_usd(
                            provider.max_input_tokens, provider.max_output_tokens
                        ),
                        "pricebook": provider.pricebook,
                    },
                },
            ],
            "features": [
                "run_pagination",
                "historical_artifacts",
                "execution_evidence",
                "recorded",
                "live",
                "structural_diff",
                "controlled_experiments",
                "hybrid_observations",
                "typed_interventions",
                "experiment_statistics",
                "static_debug_reports",
            ],
            "worker": {"status": "unknown", "reason": "no_readiness_probe"},
            "execution_environment": {"status": "unknown", "reason": "policy_is_not_readiness"},
        }

    def runs(
        self, repository_id: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        result: dict[str, Any] = public(
            self.inspection.runs(repository_id, after_id=after_id, limit=limit)
        )
        return result

    def commit_artifacts(
        self, repository_id: UUID, commit_id: str, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        result: dict[str, Any] = public(
            self.inspection.artifacts(repository_id, commit_id, after_id=after_id, limit=limit)
        )
        return result

    def executions(
        self, repository_id: UUID, run_id: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        result: dict[str, Any] = public(
            self.execution_views.list(repository_id, run_id, after_id=after_id, limit=limit)
        )
        return result

    def execution(self, repository_id: UUID, run_id: UUID, call_id: UUID) -> dict[str, Any]:
        result: dict[str, Any] = public(self.execution_views.detail(repository_id, run_id, call_id))
        return result

    def execution_content(
        self,
        repository_id: UUID,
        run_id: UUID,
        call_id: UUID,
        resource: ExecutionResource,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> tuple[dict[str, Any], bytes]:
        return self.execution_views.content(
            repository_id, run_id, call_id, resource, offset=offset, limit=limit
        )

    def replay(
        self, repository_id: UUID, payload: RecordedReplay | LiveReplay, key: str
    ) -> dict[str, Any]:
        route = "POST /replays"
        body = payload.model_dump(mode="json")
        digest = self._mutation_digest(key, repository_id, body)
        with self.history.engine.connect() as connection:
            cached = self._cached_response(connection, route, key, repository_id, digest)
        if cached is not None:
            return cached
        prepared = self.replays.prepare(repository_id, payload)
        return self._mutate(
            route,
            key,
            repository_id,
            body,
            lambda connection: self.replays.publish(
                prepared, actor=self.actor, key=key, connection=connection
            ),
        )

    def create_branch(self, repository_id: UUID, payload: NewBranch, key: str) -> dict[str, Any]:
        return self._mutate(
            "POST /branches",
            key,
            repository_id,
            payload.model_dump(mode="json"),
            lambda connection: self.versions.fork(
                repository_id,
                payload,
                actor=self.actor,
                connection=connection,
            ),
        )

    def branches(
        self, repository_id: UUID, *, after_name: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        result: dict[str, Any] = public(
            self.versions.branches(repository_id, after_name=after_name, limit=limit)
        )
        return result

    def commits(
        self,
        repository_id: UUID,
        *,
        branch_name: str = "main",
        after_commit: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.versions.log(
            repository_id,
            branch_name=branch_name,
            after_commit=after_commit,
            limit=limit,
        )

    def checkout(self, repository_id: UUID, commit_id: str) -> dict[str, Any]:
        return self.versions.checkout(repository_id, commit_id)

    def behavioral_trace(self, repository_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self.behavior.trace(repository_id, run_id).model_dump(mode="json")

    def create_debug_report(
        self, repository_id: UUID, payload: NewDebugReport, key: str
    ) -> dict[str, Any]:
        route = "POST /debug-reports"
        body = payload.model_dump(mode="json")
        digest = self._mutation_digest(key, repository_id, body)
        with self.history.engine.connect() as connection:
            cached = self._cached_response(connection, route, key, repository_id, digest)
        if cached is not None:
            return cached
        # 编译只读且有界，不持有发布事务的连接；并发编译可重复，发布仍由幂等锁串行确认。
        report = self.debug_reports.compile(repository_id, payload)
        return self._mutate(
            route,
            key,
            repository_id,
            body,
            lambda connection: self.debug_reports.store(
                repository_id, report, self.actor, connection
            ),
        )

    def debug_report(self, repository_id: UUID, identity: UUID) -> dict[str, Any]:
        return self.debug_reports.get(repository_id, identity)

    def debug_candidate_context(
        self, repository_id: UUID, identity: UUID, candidate_event_id: int, validator_ref: str
    ) -> dict[str, Any]:
        return self.debug_experiments.context(
            repository_id, identity, candidate_event_id, validator_ref
        )

    def debug_candidate_experiment(
        self, repository_id: UUID, identity: UUID, candidate_event_id: int, experiment_id: UUID
    ) -> dict[str, Any]:
        return self.debug_experiments.report(
            repository_id, identity, candidate_event_id, experiment_id
        )

    def debug_reports_list(
        self, repository_id: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        return self.debug_reports.listing(repository_id, after_id=after_id, limit=limit)

    def behavioral_diff(self, repository_id: UUID, left: UUID, right: UUID) -> dict[str, Any]:
        return self.behavior.compare(repository_id, left, right)

    def diff(
        self, repository_id: UUID, left: str, right: str, *, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        return self.versions.diff(repository_id, left, right, after=after, limit=limit)

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

    @staticmethod
    def _mutation_digest(
        key: str,
        repository_id: UUID | None,
        payload: dict[str, Any],
    ) -> str:
        if not key or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "需要有界、非空的幂等键")
        return object_digest(
            ObjectType.BLOB,
            encode(
                {"repository_id": str(repository_id) if repository_id else None, "body": payload}
            ),
        )

    def _cached_response(
        self,
        connection: Connection,
        route: str,
        key: str,
        repository_id: UUID | None,
        digest: str,
    ) -> dict[str, Any] | None:
        table = TABLES["idempotency_keys"]
        cached = (
            connection.execute(
                sa.select(table).where(
                    table.c.actor == self.actor, table.c.route == route, table.c.key == key
                )
            )
            .mappings()
            .first()
        )
        if cached is None:
            return None
        if cached["request_digest"] != digest or (
            repository_id is not None and cached["repository_id"] != repository_id
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一幂等键不能用于不同请求")
        saved = self.history.objects.get_json(cached["result_ref"], ObjectType.BLOB)
        if not isinstance(saved, dict):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "幂等响应格式无效")
        return saved

    def _mutate(
        self,
        route: str,
        key: str,
        repository_id: UUID | None,
        payload: dict[str, Any],
        action: Callable[[Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        digest = self._mutation_digest(key, repository_id, payload)
        table = TABLES["idempotency_keys"]
        with self.history.engine.begin() as connection:
            scope = encode([self.actor, route, key]).decode()
            connection.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(scope, 0)))
            )
            cached = self._cached_response(connection, route, key, repository_id, digest)
            if cached is not None:
                return cached
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
        result["model"] = config.model
        result["model_external"] = config.openai is not None
        usage = BudgetUsage()
        with self.history.engine.connect() as connection:
            snapshots, jobs = TABLES["snapshots"], TABLES["jobs"]
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
            snapshot = connection.execute(
                sa.select(snapshots.c.run_id).where(
                    snapshots.c.repository_id == repository_id,
                    snapshots.c.commit_id == row["checkpoint_commit_id"],
                    snapshots.c.run_id.in_(allowed),
                )
            ).first()
        state: AgentState | None = None
        if snapshot is not None and snapshot.run_id in allowed:
            restored = self.history.restore(repository_id, row["checkpoint_commit_id"])
            state = parse_contract(encode(restored.data), AgentState)
            usage = state.usage
        limits = self.jobs.payload(job).limits if job is not None else None
        limits = limits or config.limits
        baseline = BudgetUsage()
        if job is not None:
            spec = self.jobs.payload(job).replay
            if spec is not None and spec.budget_baseline is not None:
                baseline = spec.budget_baseline
                if snapshot is None:
                    usage = baseline
        spent = {
            name: max(0, getattr(usage, name) - getattr(baseline, name))
            for name in (
                "steps",
                "model_calls",
                "tool_calls",
                "tokens",
                "replans",
                "cost_micro_units",
            )
        }
        wall = (
            limits.max_wall_ms
            if job is None or job["deadline_at"] is None
            else max(0, int((job["deadline_at"] - now).total_seconds() * 1000))
        )
        result["budget"] = {
            "limits": limits.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
            **(
                {"baseline": baseline.model_dump(mode="json"), "new_usage": spent}
                if baseline != BudgetUsage()
                else {}
            ),
            "remaining": {
                "steps": max(0, limits.max_steps - spent["steps"]),
                "model_calls": max(0, limits.max_model_calls - spent["model_calls"]),
                "tool_calls": max(0, limits.max_tool_calls - spent["tool_calls"]),
                "tokens": max(0, limits.max_tokens - spent["tokens"] - usage.reserved_tokens),
                "wall_ms": wall,
            },
        }
        if limits.max_cost_micro_usd is not None:
            held = usage.model_meter.reserved_cost_micro_usd if usage.model_meter else 0
            if config.openai is not None:
                pending = self._pending_model_budget(repository_id, allowed, state, config)
                held = max(held, pending["cost_micro_usd"])
                result["budget"]["pending_model_reservation"] = pending
                result["budget"]["remaining"]["tokens"] = max(
                    0,
                    limits.max_tokens
                    - spent["tokens"]
                    - max(usage.reserved_tokens, pending["tokens"]),
                )
            result["budget"]["currency"] = "USD"
            result["budget"]["cost_basis"] = "pricebook_estimate"
            result["budget"]["remaining"]["cost_micro_usd"] = max(
                0, limits.max_cost_micro_usd - spent["cost_micro_units"] - held
            )
        if job is not None:
            report = self.replays.report(repository_id, job["id"])
            if report is not None:
                result["replay"] = report
        return result

    def _pending_model_budget(
        self,
        repository: UUID,
        family: set[UUID],
        state: AgentState | None,
        config: AgentConfig,
    ) -> dict[str, int]:
        observed = (
            tuple(
                trace.logical_step_id
                for trace in state.messages
                if trace.provider.startswith("llm:")
            )
            if state is not None
            else ()
        )
        calls = TABLES["external_calls"]
        with self.history.engine.connect() as connection:
            rows = list(
                connection.execute(
                    sa.select(calls.c.intent_ref)
                    .where(
                        calls.c.repository_id == repository,
                        calls.c.run_id.in_(family),
                        calls.c.provider.startswith("llm:"),
                        calls.c.logical_step_id.not_in(observed),
                    )
                    .limit(1001)
                ).scalars()
            )
        if len(rows) > 1000:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "待观察模型预算查询超过上限")
        expected = call_budget(config)
        distinct: dict[str, CallIntent] = {}
        for ref in rows:
            intent = parse_contract(self.history.objects.get(ref, ObjectType.CALL), CallIntent)
            if intent.model_budget != expected:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "待观察模型意图的费用预留不符")
            distinct[intent.occurrence.logical_step_id] = intent
        assert expected is not None
        return {
            "call_count": len(distinct),
            "tokens": len(distinct) * expected.tokens,
            "cost_micro_usd": len(distinct) * expected.cost_micro_usd,
        }

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
        if "replay" in run:
            result["replay"] = run["replay"]
            result["mode"] = run["mode"]
        return result

    def replay_status(self, repository_id: UUID, job_id: UUID) -> dict[str, Any]:
        result = self.job(repository_id, job_id)
        if "replay" not in result:
            raise AgentGitError(ErrorCode.NOT_FOUND, "重放任务不可访问")
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
            configs = TABLES["agent_configs"]
            config_version = connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(configs.c.version), 0)).where(
                    configs.c.repository_id == repository_id
                )
            ).scalar_one()
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        result: dict[str, Any] = public(dict(row))
        result["config_version"] = config_version
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
