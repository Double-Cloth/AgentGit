"""单 Job 串行 Worker；心跳与安全点检查共同保护租约。"""

import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import WriteArguments
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import CommitManifest, EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import AgentState, ResearchGoal, evolve
from agentgit.core.snapshots import EventPayload
from agentgit.runtime.ports import ModelAdapter
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES, EventInput, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.recovery import prepare_recovery, recovery_origins
from agentgit.services.research import ResearchService, ResearchSession


class LeaseHeartbeat:
    def __init__(self, queue: JobService, lease: Lease):
        self.queue, self.lease = queue, lease
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(self.lease.lease_ms / 3000):
            try:
                self.queue.heartbeat(self.lease)
            except Exception:
                self.lost.set()
                return

    def __enter__(self) -> "LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=5)


class Worker:
    def __init__(
        self,
        queue: JobService,
        *,
        name: str | None = None,
        model_factory: Callable[[], ModelAdapter] = MockResearchModel,
        fault: Callable[[str], None] | None = None,
    ):
        self.queue, self.name = queue, name or f"worker-{uuid4()}"
        self.model_factory = model_factory
        self.fault = fault or (lambda _: None)

    def run_once(self) -> dict[str, Any] | None:
        lease = self.queue.claim(self.name)
        if lease is None:
            return None
        try:
            if lease.recovery_required:
                prepared = prepare_recovery(self.queue, lease)
                if prepared is None:
                    return self._summary(lease)
                lease = prepared
            with LeaseHeartbeat(self.queue, lease) as heartbeat:
                session, origins, remaining = self._attach(lease)
                session.gateway_factory = lambda: RecordedCallGateway(
                    session.history,
                    lease.repository_id,
                    lease.run_id,
                    lambda: (session.head, session.version),
                    origin_runs=origins,
                    materialize=lambda provider, request, response: self._materialize(
                        session, provider, request, response
                    ),
                    after_result=lambda: self.fault("after_result"),
                )
                session.control_applied = lambda connection: self.queue.mark_control_applied(
                    connection, lease
                )
                session.after_checkpoint = lambda: self.fault("after_checkpoint")
                unknown = self._unresolved(lease, origins)
                if unknown:
                    session.environment = session._environment(self.model_factory())
                    if (
                        session.expected_environment is not None
                        and session.environment != session.expected_environment
                    ):
                        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "恢复环境不兼容")
                    state = self._unknown_state(session.state, unknown)
                    session.save(
                        state,
                        "run.outcome_unknown",
                        {"pending_calls": [str(call["id"]) for call in unknown]},
                    )
                else:

                    def control() -> str | None:
                        if heartbeat.lost.is_set():
                            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "Worker 心跳已失效")
                        return self.queue.apply_control(lease)

                    state = session.execute(
                        self.model_factory(), control=control, wall_budget_ms=remaining
                    )
                self.queue.finish(lease, state.status.value)
                return self._summary(lease)
        except AgentGitError as exc:
            if exc.code == ErrorCode.VERSION_CONFLICT:
                return {
                    "job_id": str(lease.job_id),
                    "run_id": str(lease.run_id),
                    "status": "lease_lost",
                    "error_code": exc.code.value,
                }
            self._fail(lease, exc)
            return self._summary(lease)

    def _summary(self, lease: Lease) -> dict[str, Any]:
        job = self.queue.get(lease.repository_id, lease.job_id)
        return {
            "job_id": str(lease.job_id),
            "run_id": str(job["run_id"]),
            "status": job["status"],
            "error_code": job["last_error"],
        }

    def _attach(self, lease: Lease) -> tuple[ResearchSession, tuple[UUID, ...], int]:
        job = self.queue.get(lease.repository_id, lease.job_id)
        payload = self.queue.payload(job)
        if payload.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Job 固定代码与 Worker 不一致")
        corpus = Corpus(payload.sources, evaluation_time=payload.evaluation_time)
        if corpus.digest() != payload.corpus_digest:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Job 语料摘要不符")
        base = self.queue.history

        def lease_guard(connection: Connection, run_id: UUID) -> None:
            self.queue.guard(connection, lease, run_id)

        history = HistoryService(
            base.engine,
            base.objects,
            fault=base.fault,
            lease_guard=lease_guard,
        )
        research = ResearchService(history, self.queue.research.data_root, corpus=corpus)
        origins = recovery_origins(self.queue, lease)
        with history.engine.begin() as connection:
            run = history._run(connection, lease.repository_id, lease.run_id, lock=True)
            job_row = self.queue.guard(connection, lease, lease.run_id)
            tasks = TABLES["tasks"]
            task = (
                connection.execute(
                    sa.select(tasks).where(
                        tasks.c.repository_id == lease.repository_id, tasks.c.id == run["task_id"]
                    )
                )
                .mappings()
                .one()
            )
            if (
                str(run["agent_config_id"]) != payload.config_id
                or task["goal_ref"] != payload.goal_ref
                or task["active_run_id"] != lease.run_id
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "Job 的任务或配置归属已改变")
            snapshots = TABLES["snapshots"]
            has_snapshot = (
                connection.execute(
                    sa.select(snapshots.c.commit_id)
                    .where(
                        snapshots.c.repository_id == lease.repository_id,
                        snapshots.c.run_id.in_((lease.run_id, *origins)),
                        snapshots.c.commit_id == run["checkpoint_commit_id"],
                    )
                    .limit(1)
                ).first()
                is not None
            )
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            remaining = max(0, int((job_row["deadline_at"] - now).total_seconds() * 1000))
        config = research.config(lease.repository_id, UUID(payload.config_id))
        goal = parse_contract(history.objects.get(payload.goal_ref, ObjectType.BLOB), ResearchGoal)
        state = AgentState(goal=goal, plan_version=payload.initial_plan_version)
        environment = None
        snapshot = None
        if has_snapshot:
            snapshot = history.restore(lease.repository_id, run["checkpoint_commit_id"])
            state = parse_contract(encode(snapshot.data), AgentState)
            if state.goal != goal:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "检查点目标与任务不符")
            manifest = parse_contract(
                history.objects.get(run["checkpoint_commit_id"], ObjectType.COMMIT), CommitManifest
            )
            environment = parse_contract(
                history.objects.get(manifest.environment_ref, ObjectType.ENVIRONMENT),
                EnvironmentManifest,
            )
        workspace = ArtifactWorkspace(
            research.data_root / "workspaces" / str(lease.run_id) / f"lease-{lease.fencing_token}",
            max_file_bytes=config.limits.max_artifact_bytes,
            max_files=config.limits.max_files,
        )
        if snapshot is not None:
            for artifact in snapshot.artifacts:
                workspace.write_bytes(
                    artifact.path,
                    history.objects.get(artifact.object_ref, ObjectType.BLOB),
                    artifact.media_type,
                )
        branch = history.branch(lease.repository_id, run["branch_name"])
        session = ResearchSession(
            research,
            lease.repository_id,
            run["task_id"],
            lease.run_id,
            config,
            state,
            workspace,
            run["checkpoint_commit_id"],
            branch["version"],
        )
        session.expected_environment, session.expected_code_revision = (
            environment,
            payload.code_revision,
        )
        # 仅更新恢复进程的操作状态；逻辑状态仍来自已校验的原检查点。
        if (
            has_snapshot
            and run["status"] == "queued"
            and state.status in (RunStatus.PLANNING, RunStatus.RUNNING)
        ):
            with history.engine.begin() as connection:
                history._run(connection, lease.repository_id, lease.run_id, lock=True)
                self.queue.guard(connection, lease, lease.run_id)
                connection.execute(
                    TABLES["runs"]
                    .update()
                    .where(TABLES["runs"].c.id == lease.run_id)
                    .values(status=state.status.value)
                )
        return session, origins, remaining

    def _unresolved(self, lease: Lease, origins: tuple[UUID, ...]) -> list[dict[str, Any]]:
        calls = TABLES["external_calls"]
        with self.queue.history.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == lease.repository_id,
                        calls.c.run_id.in_((lease.run_id, *origins)),
                        calls.c.status.in_(("started", "outcome_unknown")),
                        calls.c.result_ref.is_(None),
                    )
                ).mappings()
            ]

    def _unknown_state(self, state: AgentState, calls: list[dict[str, Any]]) -> AgentState:
        if state.status == RunStatus.OUTCOME_UNKNOWN:
            return state
        models, tools, reserved = 0, 0, 0
        attempts = dict(state.attempts)
        for call in calls:
            if call["provider"].startswith("llm:"):
                models += 1
                intent = self.queue.history.objects.get_json(call["intent_ref"], ObjectType.CALL)
                reserved += len(
                    self.queue.history.objects.get(call["request_digest"], ObjectType.BLOB)
                ) + int(intent["max_response_bytes"])
            else:
                tools += 1
                attempts[call["logical_step_id"]] = max(
                    attempts.get(call["logical_step_id"], 0), call["attempt"]
                )
        usage = evolve(
            state.usage,
            steps=state.usage.steps + models,
            model_calls=state.usage.model_calls + models,
            tool_calls=state.usage.tool_calls + tools,
            reserved_tokens=state.usage.reserved_tokens + reserved,
        )
        return evolve(
            state,
            status=RunStatus.OUTCOME_UNKNOWN,
            error_code="outcome_unknown",
            usage=usage,
            attempts=attempts,
        )

    def _materialize(
        self, session: ResearchSession, provider: str, request: bytes, response: bytes
    ) -> None:
        if provider != "tool:artifact_write/v1":
            return
        arguments = parse_contract(request, WriteArguments)
        receipt = decode(response)
        expected = object_digest(ObjectType.BLOB, arguments.text.encode("utf-8"))
        if (
            not isinstance(receipt, dict)
            or receipt.get("path") != arguments.path
            or receipt.get("content_ref") != expected
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "录制产物与原调用参数不符")
        session.workspace.write(arguments.path, arguments.text, arguments.media_type)

    def _fail(self, lease: Lease, error: AgentGitError) -> None:
        history, runs, jobs = self.queue.history, TABLES["runs"], TABLES["jobs"]
        event = EventInput("run.worker_failed", "worker", {"error": error.as_dict()})
        payload = history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
        )
        with history.engine.begin() as connection:
            run = history._run(connection, lease.repository_id, lease.run_id, lock=True)
            self.queue.guard(connection, lease, lease.run_id)
            status = "outcome_unknown" if error.code == ErrorCode.OUTCOME_UNKNOWN else "failed"
            seq = int(run["last_event_seq"]) + 1
            history._event(connection, lease.repository_id, lease.run_id, seq, event, payload)
            connection.execute(
                runs.update()
                .where(runs.c.id == lease.run_id)
                .values(status=status, failure_code=error.code.value, last_event_seq=seq)
            )
            connection.execute(
                jobs.update()
                .where(jobs.c.id == lease.job_id)
                .values(status=status, worker=None, lease_until=None, last_error=error.code.value)
            )
