"""筛选前冻结采样条件，最终候选锁后原子开启唯一确认/回归池。"""

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair_pools import (
    NewRepairValidationSuite,
    RegisteredValidationSample,
    RepairSamplingTicket,
    RepairValidationOpening,
    RepairValidationSuiteManifest,
)
from agentgit.core.repair_validation import (
    FinalRepairLock,
    RepairSamplePool,
    RepairValidationPlan,
    ValidationSample,
    validation_ref,
)
from agentgit.core.research import AgentConfig, AgentState
from agentgit.experiments.validation import frozen_inputs
from agentgit.repair.pools import validation_preflight
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


class RepairValidationPoolService:
    def __init__(self, research: ResearchService, *, fault: Callable[[str], None] | None = None):
        self.research, self.history = research, research.history
        self.studies = RepairStudyService(self.history)
        self.screening = self.studies.screening
        self.fault = fault or (lambda _: None)

    @staticmethod
    def _actor(actor: str, key: str) -> None:
        if not actor or len(actor) > 200 or not key or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "采样池操作者及幂等键必须有界非空")

    def _cached(
        self, connection: sa.Connection, table: Any, actor: str, key: str, digest: str
    ) -> Any:
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.actor == actor,
                    table.c.request_key == key,
                )
            )
            .mappings()
            .first()
        )
        if row is not None and row["request_digest"] != digest:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一采样池幂等键不能用于不同请求")
        return row

    @staticmethod
    def _suite_response(row: Any) -> dict[str, Any]:
        return dict(
            schema_version=1,
            suite_id=str(row["id"]),
            search_id=str(row["search_id"]),
            manifest_ref=row["manifest_ref"],
            response_status="not_generated",
        )

    @staticmethod
    def _opening_response(row: Any) -> dict[str, Any]:
        return dict(
            schema_version=1,
            opened=True,
            study_id=str(row["id"]),
            suite_id=str(row["suite_id"]),
            opening_ref=row["opening_ref"],
            plan_ref=row["plan_ref"],
        )

    def _source_queued(
        self, connection: sa.Connection, repository: UUID, search: UUID
    ) -> tuple[Any, Any]:
        root = self.screening._root(connection, repository, search)
        job = self.screening._job(connection, repository, search)
        trials = TABLES["repair_trials"]
        if (
            job["status"] != "queued"
            or job["revision"] != 0
            or job["cancel_requested"]
            or connection.execute(
                sa.select(trials.c.id).where(trials.c.search_id == search)
            ).first()
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "必须在筛选首次领取前冻结确认及固定回归条件")
        return root, self.screening._manifest(connection, root)

    def _origin(
        self,
        repository: UUID,
        source_run: str,
        checkpoint: str,
        role: Literal["confirm", "regression"],
        expected_config: AgentConfig,
        mode: Literal["live", "hybrid"],
    ) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            run = self.history._run(connection, repository, UUID(source_run))
            if run["status"] not in ("completed", "failed", "budget_exhausted"):
                raise AgentGitError(ErrorCode.VALIDATION, "回归及采样来源必须是有固定输入的终态")
            links = TABLES["run_commits"]
            if (
                connection.execute(
                    sa.select(links.c.commit_id).where(
                        links.c.repository_id == repository,
                        links.c.run_id == UUID(source_run),
                        links.c.commit_id == checkpoint,
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "采样起点不属于指定运行")
        view = VersionService(self.history).checkout(repository, checkpoint)
        environment = view["environment"]
        if (
            environment["code_revision"] != source_revision()
            or environment["dependency_lock_digest"] != dependency_lock_digest()
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "采样起点必须使用当前冻结代码和依赖")
        frozen = self.history.objects.get_json(environment["input_ref"], ObjectType.BLOB)
        config = parse_contract(encode(frozen["config"]), AgentConfig)
        if config != expected_config:
            raise AgentGitError(
                ErrorCode.FORBIDDEN, "固定回归必须保留原模型、配置及权限，不能另给授权"
            )
        state = parse_contract(encode(view["snapshot"]["data"]), AgentState)
        if (
            state.status.value in ("outcome_unknown", "waiting_approval", "interrupted")
            or state.pending_tool is not None
            or (state.plan is not None and not state.plan.ready(state.completed))
        ):
            raise AgentGitError(
                ErrorCode.FORBIDDEN, "采样起点必须允许新的模型决策，不能重验已结束的输出"
            )
        corpus = ReplayService(self.research)._source_corpus(repository, UUID(source_run))
        if (
            frozen.get("corpus_digest") != corpus.digest()
            or environment["evaluation_time"] != corpus.evaluation_time
            or frozen.get("goal") != state.goal.model_dump(mode="json")
        ):
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "采样来源语料、任务或时钟没有一致的冻结输入"
            )
        validator = ValidatorService(self.history).freeze(
            repository,
            state.goal,
            corpus,
            fixture_split=role,
        )
        manifest = ValidatorService(self.history).manifest(repository, validator)
        frozen_inputs(self.history.objects, manifest)
        config_body = self.history.objects.put_json(ObjectType.BLOB, config.model_dump(mode="json"))
        conditions = self.history.objects.put_json(
            ObjectType.BLOB,
            dict(
                schema_version=1,
                kind="repair_model_conditions/v1",
                model=config.model,
                openai=config.openai.model_dump(mode="json") if config.openai else None,
                generation="fresh_model_invocations/v1",
            ),
        )
        return dict(
            source_run_id=source_run,
            from_commit=checkpoint,
            task_ref=manifest.input_ref,
            validator_ref=validator,
            corpus_ref=manifest.corpus_ref,
            environment_ref=view["manifest"]["environment_ref"],
            source_state_ref=view["manifest"]["state_ref"],
            source_config_ref=config_body.id,
            model_conditions_ref=conditions.id,
            code_revision=source_revision(),
            dependency_lock_digest=dependency_lock_digest(),
            execution_mode=mode,
        )

    def submit(
        self,
        repository: UUID,
        request: NewRepairValidationSuite,
        *,
        actor: str,
        key: str,
    ) -> dict[str, Any]:
        self._actor(actor, key)
        request = parse_contract(encode(request.model_dump(mode="json")), NewRepairValidationSuite)
        search = UUID(request.search_id)
        table = TABLES["repair_validation_suites"]
        digest = object_digest(
            ObjectType.BLOB, encode([str(repository), request.model_dump(mode="json")])
        )
        with self.history.engine.connect() as connection:
            saved = self._cached(connection, table, actor, key, digest)
            if saved is not None:
                return self._suite_response(saved)
            root, source = self._source_queued(connection, repository, search)
        if source.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "旧代码搜索不能新增确认池")
        binding = source.space.binding
        base = VersionService(self.history).checkout(repository, binding.base_commit)
        frozen = self.history.objects.get_json(base["environment"]["input_ref"], ObjectType.BLOB)
        config = parse_contract(encode(frozen["config"]), AgentConfig)
        confirm = self._origin(
            repository,
            binding.failed_run_id,
            binding.base_commit,
            "confirm",
            config,
            "hybrid" if source.hybrid else "live",
        )
        screen = ValidatorService(self.history).manifest(repository, binding.validator_ref)
        if confirm["task_ref"] != screen.input_ref or confirm["corpus_ref"] != screen.corpus_ref:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "确认不能更换原筛选任务或语料")
        origins = [confirm] * 6 + [
            self._origin(
                repository,
                item.source_run_id,
                item.from_commit,
                "regression",
                config,
                "live",
            )
            for item in request.regression
        ]
        tasks = [conditions["task_ref"] for conditions in origins[6:]]
        if len(set(tasks)) != 4 or confirm["task_ref"] in tasks:
            raise AgentGitError(
                ErrorCode.VALIDATION, "固定回归必须是四个不同任务，且不同于确认任务"
            )
        registered = []
        for ordinal, conditions in enumerate(origins):
            ticket = RepairSamplingTicket(generation_id=str(uuid4()), **conditions)
            registered.append(
                RegisteredValidationSample(
                    ticket=ticket,
                    sample=ValidationSample(
                        role="confirm" if ordinal < 6 else "regression",
                        task_ref=ticket.task_ref,
                        response_set_ref=validation_ref(ticket),
                        validator_ref=ticket.validator_ref,
                    ),
                )
            )
        manifest = RepairValidationSuiteManifest(
            search_id=str(search),
            source_manifest_ref=root["manifest_ref"],
            confirm=tuple(registered[:6]),
            regression=tuple(registered[6:]),
        )
        identity = uuid4()
        with self.history.engine.begin() as connection:
            connection.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(encode([actor, "repair-suite", key]).decode(), 0),
                    )
                )
            )
            saved = self._cached(connection, table, actor, key, digest)
            if saved is not None:
                return self._suite_response(saved)
            self._source_queued(connection, repository, search)
            if source_revision() != source.code_revision:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "准备期间代码改变，未发布采样池")
            if connection.execute(
                sa.select(table.c.id).where(
                    table.c.repository_id == repository,
                    table.c.search_id == search,
                )
            ).first():
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "该搜索已有固定确认/回归池")
            body = self.history.objects.put_json(ObjectType.BLOB, manifest.model_dump(mode="json"))
            self.history._ref(
                connection, repository, body, "repair_validation_suite", str(identity), "manifest"
            )
            values = dict(
                id=identity,
                repository_id=repository,
                search_id=search,
                manifest_ref=body.id,
                actor=actor,
                request_key=key,
                request_digest=digest,
            )
            connection.execute(table.insert().values(**values))
            for ordinal, item in enumerate(registered):
                ticket_obj = self.history.objects.put_json(
                    ObjectType.BLOB, item.ticket.model_dump(mode="json")
                )
                sample = self.history.objects.put_json(
                    ObjectType.BLOB, item.sample.model_dump(mode="json")
                )
                for role, obj in (("ticket", ticket_obj), ("sample", sample)):
                    self.history._ref(
                        connection,
                        repository,
                        obj,
                        "repair_validation_sample",
                        item.ticket.generation_id,
                        role,
                    )
                for role in ("source_config_ref", "model_conditions_ref"):
                    obj = self.history.objects.put(
                        ObjectType.BLOB,
                        self.history.objects.get(
                            getattr(item.ticket, role),
                            ObjectType.BLOB,
                        ),
                    )
                    self.history._ref(
                        connection,
                        repository,
                        obj,
                        "repair_validation_sample",
                        item.ticket.generation_id,
                        role,
                    )
                connection.execute(
                    TABLES["repair_validation_samples"]
                    .insert()
                    .values(
                        id=UUID(item.ticket.generation_id),
                        repository_id=repository,
                        suite_id=identity,
                        ordinal=ordinal,
                        phase=item.sample.role,
                        generation_ref=ticket_obj.id,
                        sample_ref=sample.id,
                        source_run_id=UUID(item.ticket.source_run_id),
                        base_commit_id=item.ticket.from_commit,
                        task_ref=item.ticket.task_ref,
                        validator_ref=item.ticket.validator_ref,
                    )
                )
            self.fault("before_registration_commit")
        self.fault("after_registration_commit")
        return self._suite_response(values)

    def _suite(
        self, connection: sa.Connection, repository: UUID, identity: UUID
    ) -> tuple[Any, RepairValidationSuiteManifest]:
        table = TABLES["repair_validation_suites"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repository,
                    table.c.id == identity,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "固定确认/回归池不可访问")
        manifest = self.studies.owned(
            connection,
            repository,
            row["manifest_ref"],
            "repair_validation_suite",
            identity,
            "manifest",
            RepairValidationSuiteManifest,
        )
        samples = (
            connection.execute(
                sa.select(TABLES["repair_validation_samples"])
                .where(
                    TABLES["repair_validation_samples"].c.suite_id == identity,
                )
                .order_by(TABLES["repair_validation_samples"].c.ordinal)
            )
            .mappings()
            .all()
        )
        if manifest.search_id != str(row["search_id"]) or len(samples) != 10:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "固定池索引与 manifest 不符")
        for index, (value, item) in enumerate(
            zip(samples, (*manifest.confirm, *manifest.regression), strict=True)
        ):
            ticket = self.studies.owned(
                connection,
                repository,
                value["generation_ref"],
                "repair_validation_sample",
                value["id"],
                "ticket",
                RepairSamplingTicket,
            )
            sample = self.studies.owned(
                connection,
                repository,
                value["sample_ref"],
                "repair_validation_sample",
                value["id"],
                "sample",
                ValidationSample,
            )
            if (
                ticket != item.ticket
                or sample != item.sample
                or value["ordinal"] != index
                or value["phase"] != sample.role
                or str(value["id"]) != ticket.generation_id
                or str(value["source_run_id"]) != ticket.source_run_id
                or value["base_commit_id"] != ticket.from_commit
                or value["task_ref"] != ticket.task_ref
                or value["validator_ref"] != ticket.validator_ref
            ):
                raise AgentGitError(
                    ErrorCode.OBJECT_CORRUPT, "采样票据不能被改名、换阶段或替换索引"
                )
        return row, manifest

    def view(self, repository: UUID, identity: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            row, manifest = self._suite(connection, repository, identity)
        return {**self._suite_response(row), "manifest": manifest.model_dump(mode="json")}

    def _opening(self, connection: sa.Connection, repository: UUID, identity: UUID) -> Any:
        table = TABLES["repair_validation_openings"]
        return (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repository,
                    table.c.id == identity,
                )
            )
            .mappings()
            .first()
        )

    def opening(self, repository: UUID, study: UUID) -> dict[str, Any] | None:
        with self.history.engine.connect() as connection:
            self.studies._root(connection, repository, study)
            row = self._opening(connection, repository, study)
            if row is None:
                return None
            value = self.studies.owned(
                connection,
                repository,
                row["opening_ref"],
                "repair_study",
                study,
                "validation_opening",
                RepairValidationOpening,
            )
            if (
                value.study_id != str(study)
                or value.suite_id != str(row["suite_id"])
                or validation_ref(value.plan) != row["plan_ref"]
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "确认开启索引与冻结计划不同")
        return {**self._opening_response(row), "opening": value.model_dump(mode="json")}

    def open(
        self, repository: UUID, study: UUID, suite: UUID, *, actor: str, key: str
    ) -> dict[str, Any]:
        self._actor(actor, key)
        table = TABLES["repair_validation_openings"]
        digest = object_digest(ObjectType.BLOB, encode([str(repository), str(study), str(suite)]))
        with self.history.engine.begin() as connection:
            connection.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(
                            encode([actor, "repair-pool-open", key]).decode(), 0
                        ),
                    )
                )
            )
            cached = self._cached(connection, table, actor, key, digest)
            if cached is not None:
                return self._opening_response(cached)
            root = self.studies._root(connection, repository, study)
            job = self.studies._job(connection, repository, study)
            existing = self._opening(connection, repository, study)
            if existing is not None:
                if existing["suite_id"] != suite:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "锁定候选已打开其他固定池")
                return self._opening_response(existing)
            pool, manifest = self._suite(connection, repository, suite)
            source = self.screening._manifest(
                connection, self.screening._root(connection, repository, root["search_id"])
            )
            study_manifest = self.studies._manifest(connection, root)
            if (
                pool["search_id"] != root["search_id"]
                or manifest.source_manifest_ref != study_manifest.source_manifest_ref
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "确认池不能跨搜索或改任务复用")
            if source.code_revision != source_revision() or any(
                item.ticket.code_revision != source.code_revision
                or item.ticket.dependency_lock_digest != dependency_lock_digest()
                for item in (*manifest.confirm, *manifest.regression)
            ):
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "旧代码池只读保留，不能开启")
            if job["status"] != "awaiting_samples" or job["cancel_requested"]:
                raise AgentGitError(ErrorCode.FORBIDDEN, "必须先锁定最终候选且未取消才能打开确认池")
            decision = validation_preflight(
                source.budget, self.studies._consumption(connection, root)
            )
            if not decision.allowed:
                return dict(
                    schema_version=1,
                    opened=False,
                    study_id=str(study),
                    suite_id=str(suite),
                    decision=decision.model_dump(mode="json"),
                    reason="confirmation_reserve_insufficient",
                )
            locked = (
                connection.execute(
                    sa.select(TABLES["repair_study_locks"]).where(
                        TABLES["repair_study_locks"].c.id == study,
                    )
                )
                .mappings()
                .one()
            )
            lock = self.studies.owned(
                connection,
                repository,
                locked["lock_ref"],
                "repair_study",
                study,
                "final_lock",
                FinalRepairLock,
            )
            state = self.studies._state(connection, job)
            event_id, event_ref = self.studies._event(
                connection,
                repository,
                study,
                job["revision"] + 1,
                "sample_pool_opened",
                dict(
                    actor=actor,
                    suite_id=str(suite),
                    suite_ref=pool["manifest_ref"],
                    lock_ref=locked["lock_ref"],
                    response_status="not_generated",
                ),
            )
            plan = RepairValidationPlan(
                lock=lock,
                pool=RepairSamplePool(
                    screen_reduce_response_refs=(state.sample.response_set_ref,),
                    confirm=tuple(s.sample for s in manifest.confirm),
                    regression=tuple(s.sample for s in manifest.regression),
                ),
                opening_event_ref=event_ref,
                opened_for_lock_ref=validation_ref(lock),
            )
            opening = RepairValidationOpening(
                suite_id=str(suite), study_id=str(study), suite_ref=pool["manifest_ref"], plan=plan
            )
            for role, value in (("validation_opening", opening), ("validation_plan", plan)):
                obj = self.history.objects.put_json(ObjectType.BLOB, value.model_dump(mode="json"))
                self.history._ref(connection, repository, obj, "repair_study", str(study), role)
            values = dict(
                id=study,
                repository_id=repository,
                suite_id=suite,
                opening_ref=validation_ref(opening),
                plan_ref=validation_ref(plan),
                event_id=event_id,
                actor=actor,
                request_key=key,
                request_digest=digest,
            )
            connection.execute(table.insert().values(**values))
            connection.execute(
                TABLES["repair_study_jobs"]
                .update()
                .where(
                    TABLES["repair_study_jobs"].c.study_id == study,
                )
                .values(
                    status="validation_ready",
                    revision=job["revision"] + 1,
                    stop_reason="validation_registered_not_executed",
                )
            )
            self.fault("before_opening_commit")
        self.fault("after_opening_commit")
        return self._opening_response(values)
