"""Recorded 的冻结来源、独立任务和耐久提交；提交不执行 Runtime。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
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
from agentgit.services.history import TABLES
from agentgit.services.hybrid import observations, owned_policy, register_inputs
from agentgit.services.jobs import JobService
from agentgit.services.recordings import RecordingReader
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
from agentgit.storage.database import transaction
from agentgit.storage.roles import path_reference_role


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
        if isinstance(request, LiveReplay):
            return self._submit_live(
                repository,
                request,
                actor=actor,
                key=key,
                connection=connection,
                hybrid_ref=hybrid_ref,
            )
        if hybrid_ref is not None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 不接受 Hybrid 干预")
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
        view = self.versions.checkout(repository, request.from_commit)
        environment = parse_contract(encode(view["environment"]), EnvironmentManifest)
        initial = parse_contract(encode(view["snapshot"]["data"]), AgentState)
        frozen_input = decode(self.history.objects.get(environment.input_ref, ObjectType.BLOB))
        if not isinstance(frozen_input, dict) or "config" not in frozen_input:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源环境缺少冻结配置")
        frozen_config = parse_contract(encode(frozen_input["config"]), AgentConfig)
        baseline = (
            parse_contract(encode(frozen_input["budget_baseline"]), BudgetUsage)
            if "budget_baseline" in frozen_input
            else None
        )
        config = self.research.config(repository, source["agent_config_id"])
        if evolve(config, limits=frozen_config.limits) != frozen_config:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "来源配置与冻结输入不符")
        corpus = self._source_corpus(repository, source_id)
        selection = RecordedRange(
            source_run_id=request.source_run_id,
            from_commit=request.from_commit,
            until_commit=request.until_commit,
        )
        bundle = RecordingReader(self.history).load(
            repository, selection, expected_environment=environment
        )
        RecordedReplayer(self.history.objects.get).validate_environment(
            bundle, frozen_config, corpus, budget_baseline=baseline
        )
        research = ResearchService(self.history, self.research.data_root, corpus=corpus)
        queue = JobService(research)
        spec = RecordedReplaySpec(
            **selection.model_dump(),
            environment_ref=view["manifest"]["environment_ref"],
            checkpoint_count=len(bundle.checkpoints),
            budget_baseline=baseline,
        )
        with transaction(self.history.engine, connection) as connection:
            self.versions.fork(
                repository,
                NewBranch(
                    source_run_id=request.source_run_id,
                    from_commit=request.from_commit,
                    name=request.branch_name,
                    expected_version=0,
                ),
                actor=actor,
                connection=connection,
            )
            task_id = research.create_task(
                repository, source["agent_config_id"], initial.goal, connection=connection
            )
            submitted = queue.enqueue(
                repository,
                task_id,
                actor=actor,
                key=key,
                config_version=config_version,
                expected_branch_version=0,
                expected_head=request.from_commit,
                branch_name=request.branch_name,
                budget=frozen_config.limits,
                replay=spec,
                connection=connection,
            )
            connection.execute(
                TABLES["replay_requests"]
                .insert()
                .values(
                    repository_id=repository,
                    job_id=UUID(submitted.job_id),
                    source_run_id=source_id,
                    from_commit_id=request.from_commit,
                    until_commit_id=request.until_commit,
                    mode="recorded",
                    environment_ref=spec.environment_ref,
                )
            )
        return {
            "schema_version": 1,
            "repository_id": str(repository),
            "task_id": str(task_id),
            "job_id": submitted.job_id,
            "run_id": submitted.run_id,
            "mode": "recorded",
            "branch_name": request.branch_name,
            "source_run_id": request.source_run_id,
            "from_commit": request.from_commit,
            "until_commit": request.until_commit,
        }

    def _submit_live(
        self,
        repository: UUID,
        request: LiveReplay,
        *,
        actor: str,
        key: str,
        connection: Connection | None = None,
        hybrid_ref: str | None = None,
    ) -> dict[str, Any]:
        self.versions.select_range(
            repository,
            RecordedRange(
                source_run_id=request.source_run_id,
                from_commit=request.from_commit,
                until_commit=request.from_commit,
            ),
        )
        with self.history.engine.connect() as reading:
            source = self.history._run(reading, repository, UUID(request.source_run_id))
            if source["agent_config_id"] is None or source["task_id"] is None:
                raise AgentGitError(
                    ErrorCode.UNSUPPORTED_SCHEMA, "Live 来源不是受支持的研究 Runtime"
                )
        view = self.versions.checkout(repository, request.from_commit)
        source_environment = parse_contract(encode(view["environment"]), EnvironmentManifest)
        frozen = decode(self.history.objects.get(source_environment.input_ref, ObjectType.BLOB))
        if not isinstance(frozen, dict) or "config" not in frozen:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源缺少完整冻结配置")
        config = request.overrides.config or parse_contract(encode(frozen["config"]), AgentConfig)
        if request.budget is not None:
            if not request.budget.within(config.limits):
                raise AgentGitError(ErrorCode.FORBIDDEN, "Live 预算不能扩大新配置授权上限")
            config = parse_contract(
                encode(
                    {
                        **config.model_dump(mode="json"),
                        "limits": request.budget.model_dump(mode="json"),
                    }
                ),
                AgentConfig,
            )
        snapshot = parse_contract(encode(view["snapshot"]), StateSnapshot)
        prepared = prepare_live(snapshot, config, request.overrides, self.history.objects.get)
        corpus = self._source_corpus(repository, UUID(request.source_run_id))
        research = ResearchService(self.history, self.research.data_root, corpus=corpus)
        queue = JobService(research)
        state_obj = self.history.objects.put_json(
            ObjectType.STATE, prepared.snapshot.model_dump(mode="json")
        )
        spec_data = {
            "source_run_id": request.source_run_id,
            "from_commit": request.from_commit,
            "environment_ref": view["manifest"]["environment_ref"],
            "initial_state_ref": state_obj.id,
            "budget_baseline": prepared.budget_baseline,
        }
        spec = (
            HybridReplaySpec(**spec_data, policy_ref=hybrid_ref)
            if hybrid_ref
            else LiveReplaySpec(**spec_data)
        )
        with transaction(self.history.engine, connection) as connection:
            self.versions.fork(
                repository,
                NewBranch(
                    source_run_id=request.source_run_id,
                    from_commit=request.from_commit,
                    name=request.branch_name,
                    expected_version=0,
                ),
                actor=actor,
                connection=connection,
            )
            config_id = research.create_config(repository, config, connection=connection)
            version = connection.execute(
                sa.select(TABLES["agent_configs"].c.version).where(
                    TABLES["agent_configs"].c.id == config_id
                )
            ).scalar_one()
            state = parse_contract(encode(prepared.snapshot.data), AgentState)
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
                budget=config.limits,
                replay=spec,
                connection=connection,
            )
            self.history._ref(
                connection, repository, state_obj, "replay_input", submitted.job_id, "state"
            )
            if hybrid_ref is not None:
                register_inputs(self.history, connection, repository, submitted.job_id, hybrid_ref)
            for artifact in prepared.snapshot.artifacts:
                obj = self.history.objects.put(
                    ObjectType.BLOB, prepared.files[artifact.path], media_type=artifact.media_type
                )
                self.history._ref(
                    connection,
                    repository,
                    obj,
                    "replay_input",
                    submitted.job_id,
                    path_reference_role("file", artifact.path),
                )
            connection.execute(
                TABLES["replay_requests"]
                .insert()
                .values(
                    repository_id=repository,
                    job_id=UUID(submitted.job_id),
                    source_run_id=UUID(request.source_run_id),
                    from_commit_id=request.from_commit,
                    until_commit_id=None,
                    mode=spec.mode,
                    environment_ref=spec.environment_ref,
                )
            )
        return {
            "schema_version": 1,
            "repository_id": str(repository),
            "task_id": str(task_id),
            "job_id": submitted.job_id,
            "run_id": submitted.run_id,
            "mode": spec.mode,
            "branch_name": request.branch_name,
            "source_run_id": request.source_run_id,
            "from_commit": request.from_commit,
            "config_id": str(config_id),
            "config_version": version,
        }

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
            counts = (
                connection.execute(
                    sa.select(
                        sa.func.count().filter(calls.c.provider.like("llm:%")).label("models"),
                        sa.func.count().filter(calls.c.provider.like("tool:%")).label("tools"),
                        sa.func.count()
                        .filter(calls.c.status == "outcome_unknown")
                        .label("unknown"),
                    ).where(
                        calls.c.repository_id == repository,
                        calls.c.run_id.in_(family),
                        calls.c.origin_call_id.is_(None),
                        ~sa.exists().where(
                            hybrid.c.call_id == calls.c.id, hybrid.c.repository_id == repository
                        ),
                        calls.c.status.in_(("started", "succeeded", "failed", "outcome_unknown")),
                    )
                )
                .mappings()
                .one()
            )
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
