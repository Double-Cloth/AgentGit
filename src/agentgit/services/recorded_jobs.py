"""受租约保护的 Recorded Job；只读源录制、重算并原子保存证明。"""

from collections.abc import Callable
from time import monotonic_ns
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import evolve
from agentgit.core.snapshots import EventPayload, StateSnapshot
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RebuiltCheckpoint, RecordedReplayer
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.recordings import RecordingReader


class _Stopped(Exception):
    def __init__(self, status: str):
        self.status = status


class RecordedJobRunner:
    def __init__(self, queue: JobService, lease: Lease, fault: Callable[[str], None]):
        self.queue, self.lease, self.fault = queue, lease, fault
        base = queue.history

        def guard(connection: Connection, run_id: UUID) -> None:
            queue.guard(connection, lease, run_id)

        self.history = HistoryService(
            base.engine,
            base.objects,
            fault=base.fault,
            lease_guard=guard,
        )
        self.started = monotonic_ns()
        self.last_saved = self.started

    def _event(
        self,
        status: str,
        kind: str,
        details: dict[str, Any],
        *,
        apply: bool = False,
        error_code: str | None = None,
    ) -> None:
        event = EventInput(kind, "replay", details)
        payload = self.history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=details).model_dump(mode="json")
        )
        with self.history.engine.begin() as connection:
            run = self.history._run(
                connection, self.lease.repository_id, self.lease.run_id, lock=True
            )
            self.queue.guard(connection, self.lease, self.lease.run_id)
            seq = run["last_event_seq"] + 1
            self.history._event(
                connection, self.lease.repository_id, self.lease.run_id, seq, event, payload
            )
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(status=status, last_event_seq=seq, failure_code=error_code)
            )
            if apply:
                self.queue.mark_control_applied(connection, self.lease)

    def _control(self) -> None:
        action = self.queue.apply_control(self.lease)
        if action == "resume":
            self._event("running", "replay.resumed", {}, apply=True)
        elif action in ("pause", "cancel", "budget_exhausted"):
            raise _Stopped(
                {"pause": "paused", "cancel": "cancelled", "budget_exhausted": "budget_exhausted"}[
                    action
                ]
            )

    def run(self) -> None:
        status, failure = "completed", None
        try:
            self._control()
            self._run()
        except _Stopped as stopped:
            status = stopped.status
        except AgentGitError as error:
            if error.code == ErrorCode.VERSION_CONFLICT:
                raise
            status, failure = "failed", error
        code = (
            failure.code.value
            if failure is not None
            else (ErrorCode.BUDGET_EXHAUSTED.value if status == "budget_exhausted" else None)
        )
        details: dict[str, Any] = {
            "status": status,
            "elapsed_ms": max(0, (monotonic_ns() - self.started) // 1_000_000),
            "actual_model_calls": 0,
            "actual_tool_calls": 0,
            "actual_cost_micro_units": 0,
            "error": failure.as_dict() if failure else None,
        }
        self._event(status, "replay.attempt_finished", details, apply=True, error_code=code)
        self.fault("recorded_after_finish")
        self.queue.finish(self.lease, status)

    def _run(self) -> None:
        job = self.queue.get(self.lease.repository_id, self.lease.job_id)
        payload = self.queue.payload(job)
        spec = payload.replay
        if spec is None or payload.code_revision != source_revision():
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "Recorded Job 固定代码与当前 Worker 不符"
            )
        with self.history.engine.connect() as connection:
            request = (
                connection.execute(
                    sa.select(TABLES["replay_requests"]).where(
                        TABLES["replay_requests"].c.job_id == self.lease.job_id,
                        TABLES["replay_requests"].c.repository_id == self.lease.repository_id,
                    )
                )
                .mappings()
                .first()
            )
            proofs = list(
                connection.execute(
                    sa.select(TABLES["replay_checkpoints"])
                    .where(
                        TABLES["replay_checkpoints"].c.job_id == self.lease.job_id,
                        TABLES["replay_checkpoints"].c.repository_id == self.lease.repository_id,
                    )
                    .order_by(TABLES["replay_checkpoints"].c.source_order)
                    .limit(10002)
                ).mappings()
            )
        if request is None or any(
            request[key] != value
            for key, value in (
                ("mode", "recorded"),
                ("source_run_id", UUID(spec.source_run_id)),
                ("from_commit_id", spec.from_commit),
                ("until_commit_id", spec.until_commit),
                ("environment_ref", spec.environment_ref),
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded Job 与不可变请求不符")
        environment = parse_contract(
            self.history.objects.get(spec.environment_ref, ObjectType.ENVIRONMENT),
            EnvironmentManifest,
        )
        corpus = Corpus(payload.sources, evaluation_time=payload.evaluation_time)
        if corpus.digest() != payload.corpus_digest:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded Job 冻结语料不符")
        config = self.queue.research.config(self.lease.repository_id, UUID(payload.config_id))
        if payload.limits is not None:
            config = evolve(config, limits=payload.limits)
        bundle = RecordingReader(self.history).load(
            self.lease.repository_id,
            RecordedRange(
                source_run_id=spec.source_run_id,
                from_commit=spec.from_commit,
                until_commit=spec.until_commit,
            ),
            expected_environment=environment,
        )
        if (
            len(bundle.checkpoints) != spec.checkpoint_count
            or len(proofs) > len(bundle.checkpoints)
            or any(row["source_order"] != index for index, row in enumerate(proofs))
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 检查点证明顺序不完整")
        cursor = 0

        def save(
            snapshot: StateSnapshot, source_commit: str, event_type: str, details: dict[str, Any]
        ) -> None:
            nonlocal cursor
            state_ref = object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
            self._control()
            if cursor < len(proofs):
                proof = proofs[cursor]
                if proof["source_commit_id"] != source_commit or proof["state_ref"] != state_ref:
                    raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "已保存重放前缀与本次重算不符")
                if self.history.restore(self.lease.repository_id, proof["commit_id"]) != snapshot:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "已保存重放前缀内容不符")
                cursor += 1
                return
            run = self.queue.run(self.lease.repository_id, self.lease.run_id)
            branch = self.history.branch(self.lease.repository_id, run["branch_name"])
            now = monotonic_ns()
            elapsed = max(0, (now - self.last_saved) // 1_000_000)

            def projection(connection: Connection) -> None:
                current = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
                connection.execute(
                    TABLES["runs"]
                    .update()
                    .where(TABLES["runs"].c.id == self.lease.run_id)
                    .values(status="running")
                )
                connection.execute(
                    TABLES["replay_checkpoints"]
                    .insert()
                    .values(
                        repository_id=self.lease.repository_id,
                        job_id=self.lease.job_id,
                        run_id=self.lease.run_id,
                        source_order=cursor,
                        source_commit_id=source_commit,
                        commit_id=current["checkpoint_commit_id"],
                        state_ref=state_ref,
                        elapsed_ms=elapsed,
                    )
                )

            self.history.commit(
                self.lease.repository_id,
                self.lease.run_id,
                expected_head=run["checkpoint_commit_id"],
                expected_version=branch["version"],
                state=snapshot.data,
                environment=environment,
                events=(
                    EventInput(
                        "replay.restored" if cursor == 0 else "replay.checkpoint",
                        "replay",
                        {
                            "source_commit": source_commit,
                            "source_order": cursor,
                            "source_event_type": event_type,
                            "source_details": details,
                        },
                    ),
                ),
                artifacts=tuple(
                    ArtifactInput(
                        item.path,
                        self.history.objects.get(item.object_ref, ObjectType.BLOB),
                        item.media_type,
                    )
                    for item in snapshot.artifacts
                ),
                projection=projection,
            )
            cursor += 1
            self.last_saved = now
            self.fault("recorded_after_checkpoint")

        save(bundle.checkpoints[0][1], bundle.checkpoints[0][0], "replay.restored", {})

        def checkpoint(rebuilt: RebuiltCheckpoint) -> None:
            save(rebuilt.snapshot, rebuilt.source_commit, rebuilt.event_type, rebuilt.details)

        RecordedReplayer(self.history.objects.get).run(
            bundle, config, corpus, on_checkpoint=checkpoint
        )
        if cursor != len(bundle.checkpoints):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 没有保存全部检查点证明")
        self._control()
