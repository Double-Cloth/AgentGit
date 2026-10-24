"""Recorded 的冻结来源、独立任务和耐久提交；提交不执行 Runtime。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenCorpus
from agentgit.core.hybrid import FrozenHybridPolicy
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage, evolve
from agentgit.core.snapshots import EventPayload, StateSnapshot
from agentgit.core.versions import (
    HybridReplaySpec,
    LiveReplay,
    LiveReplaySpec,
    NewBranch,
    RecordedRange,
    RecordedReplay,
    RecordedReplaySpec,
)
from agentgit.replay.engine import RecordedReplayer
from agentgit.replay.live import prepare_live
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES
from agentgit.services.hybrid import observations, owned_policy, register_inputs
from agentgit.services.jobs import JobService
from agentgit.services.recordings import RecordingReader
from agentgit.services.replay_metering import physical_model_usage
from agentgit.services.research import ResearchService
from agentgit.services.versions import PreparedBranch, VersionService
from agentgit.storage.database import transaction
from agentgit.storage.roles import path_reference_role


@dataclass(frozen=True)
class PreparedReplay:
    repository_id: UUID
    request: RecordedReplay | LiveReplay
    branch: PreparedBranch
    config: AgentConfig
    corpus_snapshot: FrozenCorpus
    spec: RecordedReplaySpec | LiveReplaySpec | HybridReplaySpec
    code_revision: str
    source_config_id: UUID | None = None
    source_config_version: int | None = None
    files: tuple[tuple[str, str, bytes], ...] = ()
    execution_wall_ms: int | None = None
    repair_proposal_id: UUID | None = None
    repair_reduction_trial_id: UUID | None = None


class ReplayService:
    def __init__(self, research: ResearchService):
        self.research, self.history = research, research.history
        self.versions = VersionService(self.history)

    def _source_corpus(self, repository: UUID, source: UUID) -> Corpus:
        jobs = TABLES["jobs"]
        with self.history.engine.connect() as connection:
            family = self.versions._family(connection, repository, source)
            rows = list(
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.repository_id == repository,
                        sa.or_(jobs.c.run_id.in_(family), jobs.c.origin_run_id.in_(family)),
                    )
                ).mappings()
            )
        if not rows:
            return Corpus.bundled()
        positions = {run_id: index for index, run_id in enumerate(family)}

        def distance(row: Any) -> int:
            return min(positions.get(row["run_id"], 100), positions.get(row["origin_run_id"], 100))

        rows.sort(key=distance)
        if len(rows) > 1 and distance(rows[0]) == distance(rows[1]):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源 Run 没有唯一冻结 Job")
        payload = JobService(self.research).payload(rows[0])
        corpus = Corpus(payload.sources, evaluation_time=payload.evaluation_time)
        if corpus.digest() != payload.corpus_digest:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源 Job 的冻结语料不符")
        return corpus

    def submit(
        self,
        repository: UUID,
        request: RecordedReplay | LiveReplay,
        *,
        actor: str,
        key: str,
        connection: Connection | None = None,
        hybrid_ref: str | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare(repository, request, hybrid_ref=hybrid_ref)
        with transaction(self.history.engine, connection) as connection:
            return self.publish(prepared, actor=actor, key=key, connection=connection)

    def prepare(
        self,
        repository: UUID,
        request: RecordedReplay | LiveReplay,
        *,
        hybrid_ref: str | None = None,
        execution_wall_ms: int | None = None,
        repair_proposal_id: UUID | None = None,
        repair_reduction_trial_id: UUID | None = None,
    ) -> PreparedReplay:
        request = parse_contract(encode(request.model_dump(mode="json")), type(request))
        if isinstance(request, RecordedReplay) and (
            hybrid_ref or repair_proposal_id is not None or repair_reduction_trial_id is not None
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 不接受 Hybrid 干预")
        if (
            repair_proposal_id is not None or repair_reduction_trial_id is not None
        ) and hybrid_ref is None:
            raise AgentGitError(ErrorCode.VALIDATION, "Hybrid 提案归属必须绑定冻结 policy")
        if repair_proposal_id is not None and repair_reduction_trial_id is not None:
            raise AgentGitError(ErrorCode.VALIDATION, "Hybrid 输入只能有一个精确派发归属")
        if execution_wall_ms is not None and (
            type(execution_wall_ms) is not int
            or execution_wall_ms < 1
            or not isinstance(request, RecordedReplay)
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "独立执行期限仅适用于 Recorded")
        source_id = UUID(request.source_run_id)
        with self.history.engine.connect() as reading:
            source = dict(self.history._run(reading, repository, source_id))
            if source["agent_config_id"] is None or source["task_id"] is None:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "来源不是受支持的研究 Runtime")
            config_version = reading.execute(
                sa.select(TABLES["agent_configs"].c.version).where(
                    TABLES["agent_configs"].c.repository_id == repository,
                    TABLES["agent_configs"].c.id == source["agent_config_id"],
                )
            ).scalar_one()
        branch = self.versions.prepare_fork(
            repository,
            NewBranch(
                source_run_id=request.source_run_id,
                from_commit=request.from_commit,
                name=request.branch_name,
                expected_version=0,
            ),
        )
        view = self.versions.checkout(repository, request.from_commit)
        environment = parse_contract(encode(view["environment"]), EnvironmentManifest)
        snapshot = parse_contract(branch.snapshot_payload, StateSnapshot)
        frozen = decode(self.history.objects.get(environment.input_ref, ObjectType.BLOB))
        if not isinstance(frozen, dict) or "config" not in frozen:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源环境缺少完整冻结配置")
        frozen_config = parse_contract(encode(frozen["config"]), AgentConfig)
        corpus = self._source_corpus(repository, source_id)
        corpus_snapshot = FrozenCorpus(
            evaluation_time=corpus.evaluation_time, sources=tuple(corpus.sources.values())
        )
        corpus = Corpus(corpus_snapshot.sources, evaluation_time=corpus_snapshot.evaluation_time)
        if isinstance(request, RecordedReplay):
            baseline = (
                parse_contract(encode(frozen["budget_baseline"]), BudgetUsage)
                if "budget_baseline" in frozen
                else None
            )
            config = self.research.config(repository, source["agent_config_id"])
            if evolve(config, limits=frozen_config.limits) != frozen_config:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "来源配置与冻结输入不符")
            if (
                execution_wall_ms is not None
                and execution_wall_ms > frozen_config.limits.max_wall_ms
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 实际执行期限不能扩大配置上限")
            selection = RecordedRange(
                source_run_id=request.source_run_id,
                from_commit=request.from_commit,
                until_commit=request.until_commit,
            )
            bundle = RecordingReader(self.history).load(
                repository, selection, expected_environment=environment
            )
            RecordedReplayer(self.history.objects.get).validate_environment(
                bundle,
                frozen_config,
                corpus,
                budget_baseline=baseline,
            )
            spec = RecordedReplaySpec(
                **selection.model_dump(),
                environment_ref=view["manifest"]["environment_ref"],
                checkpoint_count=len(bundle.checkpoints),
                budget_baseline=baseline,
            )
            return PreparedReplay(
                repository,
                request,
                branch,
                frozen_config,
                corpus_snapshot,
                spec,
                source_revision(),
                source_config_id=source["agent_config_id"],
                source_config_version=config_version,
                execution_wall_ms=execution_wall_ms,
            )
        config = request.overrides.config or frozen_config
        if request.budget is not None:
            if not request.budget.within(config.limits):
                raise AgentGitError(ErrorCode.FORBIDDEN, "Live 预算不能扩大新配置授权上限")
            config = evolve(config, limits=request.budget)
        live = prepare_live(snapshot, config, request.overrides, self.history.objects.get)
        state_obj = self.history.objects.put_json(
            ObjectType.STATE, live.snapshot.model_dump(mode="json")
        )
        spec_data = dict(
            source_run_id=request.source_run_id,
            from_commit=request.from_commit,
            environment_ref=view["manifest"]["environment_ref"],
            initial_state_ref=state_obj.id,
            budget_baseline=live.budget_baseline,
        )
        live_spec = (
            HybridReplaySpec(**spec_data, policy_ref=hybrid_ref)
            if hybrid_ref
            else LiveReplaySpec(**spec_data)
        )
        if hybrid_ref is not None:
            policy = parse_contract(
                self.history.objects.get(hybrid_ref, ObjectType.BLOB), FrozenHybridPolicy
            )
            if (
                policy.source_run_id != request.source_run_id
                or policy.from_commit != request.from_commit
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Hybrid policy 与提交来源或起点不同")
        files = tuple(
            (item.path, item.media_type, live.files[item.path]) for item in live.snapshot.artifacts
        )
        return PreparedReplay(
            repository,
            request,
            branch,
            config,
            corpus_snapshot,
            live_spec,
            source_revision(),
            files=files,
            repair_proposal_id=repair_proposal_id,
            repair_reduction_trial_id=repair_reduction_trial_id,
        )

    def publish(
        self,
        prepared: PreparedReplay,
        *,
        actor: str,
        key: str,
        connection: Connection,
    ) -> dict[str, Any]:
        if prepared.code_revision != source_revision():
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "Replay 准备与发布之间的代码版本不兼容"
            )
        repository, request, spec = prepared.repository_id, prepared.request, prepared.spec
        research = ResearchService(
            self.history,
            self.research.data_root,
            corpus=Corpus(
                prepared.corpus_snapshot.sources,
                evaluation_time=prepared.corpus_snapshot.evaluation_time,
            ),
        )
        queue = JobService(research)
        self.versions.publish_fork(prepared.branch, actor=actor, connection=connection)
        if isinstance(spec, RecordedReplaySpec):
            config_id, version = prepared.source_config_id, prepared.source_config_version
            if config_id is None or version is None:
                raise AgentGitError(ErrorCode.INTERNAL_ERROR, "Recorded 准备缺少冻结配置身份")
            snapshot = parse_contract(prepared.branch.snapshot_payload, StateSnapshot)
        else:
            config_id = research.create_config(repository, prepared.config, connection=connection)
            version = connection.execute(
                sa.select(TABLES["agent_configs"].c.version).where(
                    TABLES["agent_configs"].c.id == config_id
                )
            ).scalar_one()
            snapshot = parse_contract(
                self.history.objects.get(spec.initial_state_ref, ObjectType.STATE), StateSnapshot
            )
        state = parse_contract(encode(snapshot.data), AgentState)
        task_id = research.create_task(repository, config_id, state.goal, connection=connection)
        submitted = queue.enqueue(
            repository,
            task_id,
            actor=actor,
            key=key,
            config_version=version,
            expected_branch_version=0,
            expected_head=request.from_commit,
            branch_name=request.branch_name,
            budget=prepared.config.limits,
            replay=spec,
            connection=connection,
            execution_wall_ms=prepared.execution_wall_ms,
        )
        if not isinstance(spec, RecordedReplaySpec):
            state_obj = self.history.objects.put(
                ObjectType.STATE, self.history.objects.get(spec.initial_state_ref, ObjectType.STATE)
            )
            self.history._ref(
                connection, repository, state_obj, "replay_input", submitted.job_id, "state"
            )
            if isinstance(spec, HybridReplaySpec):
                register_inputs(
                    self.history,
                    connection,
                    repository,
                    submitted.job_id,
                    spec.policy_ref,
                    repair_proposal_id=prepared.repair_proposal_id,
                    repair_reduction_trial_id=prepared.repair_reduction_trial_id,
                )
            for path, media_type, content in prepared.files:
                obj = self.history.objects.put(ObjectType.BLOB, content, media_type=media_type)
                self.history._ref(
                    connection,
                    repository,
                    obj,
                    "replay_input",
                    submitted.job_id,
                    path_reference_role("file", path),
                )
        connection.execute(
            TABLES["replay_requests"]
            .insert()
            .values(
                repository_id=repository,
                job_id=UUID(submitted.job_id),
                source_run_id=UUID(request.source_run_id),
                from_commit_id=request.from_commit,
                until_commit_id=spec.until_commit if isinstance(spec, RecordedReplaySpec) else None,
                mode=spec.mode,
                environment_ref=spec.environment_ref,
            )
        )
        result = {
            "schema_version": 1,
            "repository_id": str(repository),
            "task_id": str(task_id),
            "job_id": submitted.job_id,
            "run_id": submitted.run_id,
            "mode": spec.mode,
            "branch_name": request.branch_name,
            "source_run_id": request.source_run_id,
            "from_commit": request.from_commit,
        }
        if isinstance(spec, RecordedReplaySpec):
            result["until_commit"] = spec.until_commit
        else:
            result.update(config_id=str(config_id), config_version=version)
        return result

    def _live_report(
        self, repository: UUID, job: dict[str, Any], spec: LiveReplaySpec | HybridReplaySpec
    ) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            run = self.history._run(connection, repository, job["run_id"])
            family = [job["run_id"]]
            root, current = job["origin_run_id"], run["origin_run_id"]
            while root is not None and family[-1] != root:
                if current is None or current in family or len(family) >= 100:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Live 计量来源链非法")
                family.append(current)
                current = self.history._run(connection, repository, current)["origin_run_id"]
            calls = TABLES["external_calls"]
            hybrid = TABLES["hybrid_observations"]
            physical = (
                calls.c.repository_id == repository,
                calls.c.run_id.in_(family),
                calls.c.origin_call_id.is_(None),
                ~sa.exists().where(
                    hybrid.c.call_id == calls.c.id, hybrid.c.repository_id == repository
                ),
                calls.c.status.in_(("started", "succeeded", "failed", "outcome_unknown")),
            )
            counts = (
                connection.execute(
                    sa.select(
                        sa.func.count().filter(calls.c.provider.like("llm:%")).label("models"),
                        sa.func.count().filter(calls.c.provider.like("tool:%")).label("tools"),
                        sa.func.count()
                        .filter(calls.c.status == "outcome_unknown")
                        .label("unknown"),
                    ).where(
                        *physical,
                    )
                )
                .mappings()
                .one()
            )
            objects = TABLES["objects"]
            if counts["models"] > 1000:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "本次真实模型调用超过冻结预算上限")
            model_rows = list(
                connection.execute(
                    sa.select(calls, objects.c.byte_length.label("input_bytes"))
                    .select_from(calls.outerjoin(objects, objects.c.id == calls.c.request_digest))
                    .where(*physical, calls.c.provider.like("llm:%"))
                    .order_by(calls.c.id)
                    .limit(1001)
                ).mappings()
            )
            if len(model_rows) != counts["models"]:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "模型调用账本正在变化，请刷新计量")
            events = TABLES["events"]
            last = connection.execute(
                sa.select(sa.func.max(events.c.recorded_at)).where(
                    events.c.repository_id == repository, events.c.run_id.in_(family)
                )
            ).scalar_one()
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        initialized = (
            job["checkpoint_commit_id"] is not None
            and job["checkpoint_commit_id"] != spec.from_commit
        )
        state = (
            parse_contract(
                encode(self.history.restore(repository, job["checkpoint_commit_id"]).data),
                AgentState,
            )
            if initialized
            else None
        )
        usage = state.usage if state else spec.budget_baseline
        delta = {
            name: max(0, getattr(usage, name) - getattr(spec.budget_baseline, name))
            for name in (
                "steps",
                "model_calls",
                "tool_calls",
                "tokens",
                "replans",
                "cost_micro_units",
            )
        }
        end = now if job["status"] in ("running", "queued") else last or now
        wall = (
            max(0, int((end - job["started_at"]).total_seconds() * 1000))
            if job["started_at"]
            else 0
        )
        result: dict[str, Any] = {
            "mode": spec.mode,
            "source_run_id": spec.source_run_id,
            "from_commit": spec.from_commit,
            "initialized": initialized,
            "logical_status": state.status.value if state else None,
            "logical_usage": usage.model_dump(mode="json"),
            "budget_baseline": spec.budget_baseline.model_dump(mode="json"),
            "new_usage": delta,
            "actual": {
                "model_calls": counts["models"],
                "tool_calls": counts["tools"],
                "unknown_outcomes": counts["unknown"],
                "wall_ms": wall,
                "cost_micro_units": delta["cost_micro_units"],
                "model_usage": physical_model_usage(
                    self.history, repository, [dict(row) for row in model_rows]
                ),
                **(
                    {"currency": "USD", "cost_basis": "pricebook_estimate"}
                    if usage.model_meter is not None
                    else {}
                ),
            },
        }
        if isinstance(spec, HybridReplaySpec):
            items = observations(self.history, repository, job["id"])
            result["observations"] = items
            result["test_only"] = True
            result["policy_ref"] = spec.policy_ref
            policy = owned_policy(self.history, repository, job["id"], spec.policy_ref)
            reached = {item["selected_call_id"] for item in items}
            result["hybrid"] = {
                "miss_policy": policy.miss_policy,
                "selected_call_ids": [call.source_call_id for call in policy.calls],
                "unreached_call_ids": [
                    call.source_call_id
                    for call in policy.calls
                    if call.source_call_id not in reached
                ],
            }
            result["actual"]["fixture_calls"] = sum(item["kind"] == "fixture" for item in items)
            result["actual"]["recorded_observations"] = sum(
                item["kind"] == "recorded" for item in items
            )
            result["actual"]["synthetic_observations"] = sum(
                item["kind"] == "synthetic" for item in items
            )
            result["actual"]["hybrid_misses"] = sum(item["kind"] == "miss" for item in items)
        return result

    def report(self, repository: UUID, job_id: UUID) -> dict[str, Any] | None:
        queue = JobService(self.research)
        job = queue.get(repository, job_id)
        spec = queue.payload(job).replay
        if spec is None:
            return None
        if isinstance(spec, (LiveReplaySpec, HybridReplaySpec)):
            return self._live_report(repository, job, spec)
        with self.history.engine.connect() as connection:
            run = self.history._run(connection, repository, job["run_id"])
            family = [job["run_id"]]
            root = job["origin_run_id"]
            current = run["origin_run_id"]
            while root is not None and family[-1] != root:
                if current is None or current in family or len(family) >= 100:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "重放计量来源链非法")
                family.append(current)
                current = self.history._run(connection, repository, current)["origin_run_id"]
            points = TABLES["replay_checkpoints"]
            proofs = list(
                connection.execute(
                    sa.select(points)
                    .where(
                        points.c.repository_id == repository,
                        points.c.job_id == job_id,
                    )
                    .order_by(points.c.source_order)
                    .limit(10002)
                ).mappings()
            )
            events = TABLES["events"]
            measured = list(
                connection.execute(
                    sa.select(events.c.payload_ref)
                    .where(
                        events.c.repository_id == repository,
                        events.c.run_id.in_(family),
                        events.c.type == "replay.attempt_finished",
                    )
                    .limit(10001)
                ).scalars()
            )
        if (
            len(proofs) > spec.checkpoint_count
            or len(measured) > 10000
            or any(row["source_order"] != index for index, row in enumerate(proofs))
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "重放证明或计量数量不符")
        elapsed = 0
        for reference in measured:
            data = parse_contract(
                self.history.objects.get(reference, ObjectType.EVENT), EventPayload
            ).data
            if (
                type(data.get("elapsed_ms")) is not int
                or data["elapsed_ms"] < 0
                or any(
                    type(data.get(name)) is not int or data[name] != 0
                    for name in (
                        "actual_model_calls",
                        "actual_tool_calls",
                        "actual_cost_micro_units",
                    )
                )
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 实际计量不符合零调用事实")
            elapsed += data["elapsed_ms"]
            if elapsed > 9007199254740991:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "重放累计耗时超过整数上限")
        logical = None
        if proofs:
            snapshot = self.history.restore(repository, proofs[-1]["commit_id"])
            logical = parse_contract(encode(snapshot.data), AgentState)
        reached = bool(proofs and proofs[-1]["source_commit_id"] == spec.until_commit)
        result: dict[str, Any] = {
            "mode": "recorded",
            "source_run_id": spec.source_run_id,
            "from_commit": spec.from_commit,
            "until_commit": spec.until_commit,
            "restored": bool(proofs),
            "recomputed_checkpoints": max(0, len(proofs) - 1),
            "expected_checkpoints": spec.checkpoint_count - 1,
            "last_source_commit": proofs[-1]["source_commit_id"] if proofs else None,
            "target_reached": reached,
            "verified": reached and job["status"] == "completed",
            "logical_status": logical.status.value if logical else None,
            "logical_usage": logical.usage.model_dump(mode="json") if logical else None,
            "actual": {
                "elapsed_ms": elapsed,
                "unmeasured_attempts": max(0, job["attempt"] - len(measured)),
                "model_calls": 0,
                "tool_calls": 0,
                "cost_micro_units": 0,
            },
        }
        environment = parse_contract(
            self.history.objects.get(spec.environment_ref, ObjectType.ENVIRONMENT),
            EnvironmentManifest,
        )
        frozen_input = self.history.objects.get_json(environment.input_ref, ObjectType.BLOB)
        reference = (
            frozen_input.get("hybrid_policy_ref") if isinstance(frozen_input, dict) else None
        )
        if reference is not None:
            policy = parse_contract(
                self.history.objects.get(reference, ObjectType.BLOB), FrozenHybridPolicy
            )
            result["test_only"] = True
            result["source_hybrid_policy_ref"] = reference
            result["source_has_synthetic_patch"] = any(
                call.intervention_id is not None for call in policy.calls
            )
        return result
