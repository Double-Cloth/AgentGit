"""Recorded 的冻结来源、独立任务和耐久提交；提交不执行 Runtime。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, evolve
from agentgit.core.snapshots import EventPayload
from agentgit.core.versions import NewBranch, RecordedRange, RecordedReplay, RecordedReplaySpec
from agentgit.replay.engine import RecordedReplayer
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.recordings import RecordingReader
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
from agentgit.storage.database import transaction


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
        request: RecordedReplay,
        *,
        actor: str,
        key: str,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        source_id = UUID(request.source_run_id)
        with self.history.engine.connect() as reading:
            source = dict(self.history._run(reading, repository, source_id))
            if source["agent_config_id"] is None or source["task_id"] is None:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "来源不是受支持的研究 Runtime")
            if source["mode"] == "recorded":
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "复合 Recorded 来源尚未接通")
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
            bundle, frozen_config, corpus
        )
        research = ResearchService(self.history, self.research.data_root, corpus=corpus)
        queue = JobService(research)
        spec = RecordedReplaySpec(
            **selection.model_dump(),
            environment_ref=view["manifest"]["environment_ref"],
            checkpoint_count=len(bundle.checkpoints),
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

    def report(self, repository: UUID, job_id: UUID) -> dict[str, Any] | None:
        queue = JobService(self.research)
        job = queue.get(repository, job_id)
        spec = queue.payload(job).replay
        if spec is None:
            return None
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
        return {
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
