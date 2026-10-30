"""单 Job 串行 Worker；心跳与安全点检查共同保护租约。"""

import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mcp import MCPClient
from agentgit.adapters.models import select_model
from agentgit.adapters.tools import WriteArguments
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.mcp import MCPBinding
from agentgit.core.models import (
    CallIntent,
    CommitManifest,
    EnvironmentManifest,
    RunStatus,
    parse_contract,
)
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage, ResearchGoal, evolve
from agentgit.core.snapshots import EventPayload, StateSnapshot
from agentgit.core.versions import HybridReplaySpec
from agentgit.runtime.collaboration_run import CollaborationRoleRunner
from agentgit.runtime.langgraph_run import LangGraphRunRunner
from agentgit.runtime.mcp_run import MCPRunRunner
from agentgit.runtime.model_budget import call_budget, reserve_model
from agentgit.runtime.ports import CallSettlement, ModelAdapter
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.collaboration_control import CollaborationCoordinator
from agentgit.services.environment import source_revision
from agentgit.services.execution_recovery import discard_stopped, occurrence_of, validate_retry
from agentgit.services.executions import ExecutionService
from agentgit.services.history import TABLES, EventInput, HistoryService
from agentgit.services.hybrid import HybridService
from agentgit.services.jobs import JobService
from agentgit.services.merge_runs import load_merged_input
from agentgit.services.recorded_jobs import RecordedJobRunner
from agentgit.services.recovery import prepare_recovery, recovery_origins
from agentgit.services.research import ResearchService, ResearchSession
from agentgit.storage.roles import path_reference_role


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
        model_factory: Callable[[], ModelAdapter] | None = None,
        fault: Callable[[str], None] | None = None,
        mcp_client_factory: Callable[[MCPBinding], MCPClient] | None = None,
        collaboration_model_factory: Callable[[str], ModelAdapter] | None = None,
    ):
        self.queue, self.name = queue, name or f"worker-{uuid4()}"
        self.model_factory = model_factory
        self.fault = fault or (lambda _: None)
        self.mcp_client_factory = mcp_client_factory
        self.collaboration_model_factory = collaboration_model_factory

    def run_once(self) -> dict[str, Any] | None:
        ApprovalService.expire_pending(self.queue.history)
        CollaborationCoordinator(self.queue).tick()
        lease = self.queue.claim(self.name)
        if lease is None:
            return None
        try:
            job = self.queue.get(lease.repository_id, lease.job_id)
            if job["kind"] == "collaboration_role":
                with LeaseHeartbeat(self.queue, lease):
                    return CollaborationRoleRunner(
                        self.queue,
                        lease,
                        fault=self.fault,
                        model_factory=self.collaboration_model_factory,
                    ).run()
            if job["kind"] == "langgraph_run":
                with LeaseHeartbeat(self.queue, lease):
                    return LangGraphRunRunner(
                        self.queue, lease, fault=self.fault, client_factory=self.mcp_client_factory
                    ).run()
            if job["kind"] == "mcp_run":
                with LeaseHeartbeat(self.queue, lease):
                    return MCPRunRunner(
                        self.queue, lease, fault=self.fault, client_factory=self.mcp_client_factory
                    ).run()
            payload = self.queue.payload(job)
            if payload.replay is not None and payload.replay.mode == "recorded":
                if lease.recovery_required:
                    with LeaseHeartbeat(self.queue, lease):
                        prepared = prepare_recovery(self.queue, lease)
                    if prepared is None:
                        return self._summary(lease)
                    lease = prepared
                with LeaseHeartbeat(self.queue, lease):
                    self.fault("after_claim")
                    RecordedJobRunner(self.queue, lease, self.fault).run()
                return self._summary(lease)
            if lease.recovery_required:
                with LeaseHeartbeat(self.queue, lease):
                    ExecutionService.reconcile(self.queue, lease)
                    prepared = prepare_recovery(self.queue, lease)
                if prepared is None:
                    return self._summary(lease)
                lease = prepared
            self.fault("after_claim")
            with LeaseHeartbeat(self.queue, lease) as heartbeat:
                session, origins, remaining = self._attach(lease)
                hybrid = None
                replay = payload.replay
                if isinstance(replay, HybridReplaySpec):
                    hybrid = HybridService(
                        session.history, lease, replay.policy_ref, session.service.corpus
                    )
                    if (
                        hybrid.policy.source_run_id != replay.source_run_id
                        or hybrid.policy.from_commit != replay.from_commit
                    ):
                        raise AgentGitError(
                            ErrorCode.OBJECT_CORRUPT, "Hybrid policy 与冻结重执行来源不符"
                        )
                    session.hybrid_policy_ref = replay.policy_ref
                    session.before_evaluate = hybrid.require_reached
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
                    hybrid=hybrid,
                )
                session.control_applied = lambda connection: self.queue.mark_control_applied(
                    connection, lease
                )
                session.after_checkpoint = lambda: self.fault("after_checkpoint")
                unknown = self._unresolved(lease, origins, session)
                if unknown:
                    session.environment = session._environment(self._model(session.config))
                    if (
                        session.expected_environment is not None
                        and session.environment != session.expected_environment
                    ):
                        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "恢复环境不兼容")
                    if discard_stopped(session, self.queue, lease, origins, unknown, self.fault):
                        unknown = []
                if unknown:
                    state = self._unknown_state(session.state, unknown, session.config)
                    session.save(
                        state,
                        "run.outcome_unknown",
                        {"pending_calls": [str(call["id"]) for call in unknown]},
                    )
                else:
                    if session.state.status == RunStatus.OUTCOME_UNKNOWN:
                        session.settlement = self._settlement(session, lease, origins)

                    def control() -> str | None:
                        if heartbeat.lost.is_set():
                            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "Worker 心跳已失效")
                        return self.queue.apply_control(lease)

                    state = session.execute(
                        self._model(session.config), control=control, wall_budget_ms=remaining
                    )
                self.queue.finish(lease, state.status.value)
                return self._summary(lease)
        except AgentGitError as exc:
            if exc.code == ErrorCode.APPROVAL_REQUIRED:
                return self._summary(lease)
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
        if payload.limits is not None:
            config = parse_contract(
                encode(
                    {
                        **config.model_dump(mode="json"),
                        "limits": payload.limits.model_dump(mode="json"),
                    }
                ),
                AgentConfig,
            )
        goal = parse_contract(history.objects.get(payload.goal_ref, ObjectType.BLOB), ResearchGoal)
        state = AgentState(goal=goal, plan_version=payload.initial_plan_version)
        environment = None
        snapshot = None
        live = (
            payload.replay
            if payload.replay is not None and payload.replay.mode in ("live", "hybrid")
            else None
        )
        merged_snapshot = (
            load_merged_input(
                history, lease.repository_id, lease.job_id, payload.merged, config, corpus
            )
            if payload.merged is not None
            else None
        )
        if live is not None:
            with history.engine.connect() as connection:
                request = (
                    connection.execute(
                        sa.select(TABLES["replay_requests"]).where(
                            TABLES["replay_requests"].c.repository_id == lease.repository_id,
                            TABLES["replay_requests"].c.job_id == lease.job_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if request is None or any(
                    request[key] != value
                    for key, value in (
                        ("mode", live.mode),
                        ("source_run_id", UUID(live.source_run_id)),
                        ("from_commit_id", live.from_commit),
                        ("environment_ref", live.environment_ref),
                    )
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Live Job 与不可变来源不符")
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
        elif merged_snapshot is not None and payload.merged is not None:
            if run["checkpoint_commit_id"] != payload.merged.from_commit:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并新 Run 初始检查点不符")
            snapshot = merged_snapshot
            state = parse_contract(encode(snapshot.data), AgentState)
            if state.goal != goal:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "合并输入与新任务目标不符")
        elif live is not None:
            if run["checkpoint_commit_id"] != live.from_commit:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Live 初始检查点与冻结来源不符")
            with history.engine.connect() as connection:
                refs = TABLES["object_refs"]
                owned = connection.execute(
                    sa.select(refs.c.object_id).where(
                        refs.c.repository_id == lease.repository_id,
                        refs.c.owner_type == "replay_input",
                        refs.c.owner_id == str(lease.job_id),
                        refs.c.role == "state",
                        refs.c.object_id == live.initial_state_ref,
                    )
                ).first()
            if owned is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "Live 初始状态缺少当前 Job 归属")
            snapshot = parse_contract(
                history.objects.get(live.initial_state_ref, ObjectType.STATE), StateSnapshot
            )
            state = parse_contract(encode(snapshot.data), AgentState)
            if state.goal != goal or state.usage != live.budget_baseline:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Live 初始目标或预算基线不符")
            with history.engine.connect() as connection:
                for artifact in snapshot.artifacts:
                    owned = connection.execute(
                        sa.select(refs.c.object_id).where(
                            refs.c.repository_id == lease.repository_id,
                            refs.c.owner_type == "replay_input",
                            refs.c.owner_id == str(lease.job_id),
                            refs.c.role == path_reference_role("file", artifact.path),
                            refs.c.object_id == artifact.object_ref,
                        )
                    ).first()
                    if owned is None:
                        raise AgentGitError(ErrorCode.FORBIDDEN, "Live 初始产物缺少当前 Job 归属")
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
        if live is not None:
            session.budget_baseline = live.budget_baseline
            if not has_snapshot:
                session.initialization = {
                    "source_run_id": live.source_run_id,
                    "from_commit": live.from_commit,
                    "initial_state_ref": live.initial_state_ref,
                }
        if payload.merged is not None:
            session.budget_baseline = BudgetUsage()
            if not has_snapshot:
                session.initialization_type = "merge.initialized"
                session.initialization = payload.merged.model_dump(mode="json")
        if config.execution_policy is not None:
            session.bind_execution(
                ExecutionService(
                    history, self.queue, lease, config.execution_policy, fault=self.fault
                )
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

    def _unresolved(
        self, lease: Lease, origins: tuple[UUID, ...], session: ResearchSession
    ) -> list[dict[str, Any]]:
        calls = TABLES["external_calls"]
        with session.history.engine.begin() as connection:
            session.history._run(connection, lease.repository_id, lease.run_id, lock=True)
            session.history.assert_writer(connection, lease.repository_id, lease.run_id)
            self.queue.guard(connection, lease, lease.run_id)
            rows = [
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
            stopped = {item.occurrence: item for item in session.state.stopped_executions}
            pending = []
            for row in rows:
                previous = stopped.get(occurrence_of(row))
                if previous is None:
                    pending.append(row)
                else:
                    if session.config.execution_policy is None:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "停止执行记录缺少固定策略")
                    validate_retry(
                        connection,
                        session.history,
                        lease,
                        (lease.run_id, *origins),
                        previous,
                        session.config.execution_policy,
                    )
            return pending

    def _model(self, config: AgentConfig) -> ModelAdapter:
        return self.model_factory() if self.model_factory is not None else select_model(config)

    def _unknown_state(
        self,
        state: AgentState,
        calls: list[dict[str, Any]],
        config: AgentConfig,
    ) -> AgentState:
        if state.status == RunStatus.OUTCOME_UNKNOWN:
            return state
        tools = 0
        usage = state.usage
        attempts = dict(state.attempts)
        for call in calls:
            if call["provider"].startswith("llm:"):
                intent = parse_contract(
                    self.queue.history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
                )
                if intent.model_budget != call_budget(config):
                    raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知模型意图预留与配置不符")
                usage = reserve_model(
                    usage,
                    config,
                    len(self.queue.history.objects.get(call["request_digest"], ObjectType.BLOB)),
                )
            else:
                tools += 1
                attempts[call["logical_step_id"]] = max(
                    attempts.get(call["logical_step_id"], 0), call["attempt"]
                )
        usage = evolve(
            usage,
            tool_calls=usage.tool_calls + tools,
        )
        return evolve(
            state,
            status=RunStatus.OUTCOME_UNKNOWN,
            resume_status=state.status,
            error_code="outcome_unknown",
            usage=usage,
            attempts=attempts,
        )

    def _settlement(
        self, session: ResearchSession, lease: Lease, origins: tuple[UUID, ...]
    ) -> CallSettlement:
        resolutions, calls = TABLES["call_resolutions"], TABLES["external_calls"]
        with session.history.engine.connect() as connection:
            rows = list(
                connection.execute(
                    sa.select(
                        calls,
                        resolutions.c.checkpoint_commit_id.label("resolved_checkpoint"),
                        resolutions.c.result_ref.label("resolved_result"),
                        resolutions.c.evidence_ref,
                        resolutions.c.request_digest.label("resolved_request"),
                        resolutions.c.resolver_signature,
                    )
                    .join(resolutions, resolutions.c.call_id == calls.c.id)
                    .where(
                        resolutions.c.repository_id == lease.repository_id,
                        resolutions.c.run_id.in_((lease.run_id, *origins)),
                        calls.c.run_id.in_((lease.run_id, *origins)),
                        resolutions.c.checkpoint_commit_id == session.head,
                    )
                ).mappings()
            )
        if len(rows) != 1:
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "当前停止点没有唯一的可信核对结果")
        call = rows[0]
        intent = parse_contract(
            session.history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
        )
        if (
            call["status"] != "succeeded"
            or call["result_ref"] is None
            or call["result_ref"] != call["resolved_result"]
            or call["request_digest"] != call["resolved_request"]
            or call["signature_digest"] != call["resolver_signature"]
            or intent.checkpoint is None
            or intent.request_ref != call["request_digest"]
            or intent.effect.value != call["effect"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对结果不完整")
        session.history.objects.get(call["evidence_ref"], ObjectType.BLOB)
        before = parse_contract(
            encode(session.history.restore(lease.repository_id, intent.checkpoint).data),
            AgentState,
        )
        occurrence = intent.occurrence
        if any(
            occurrence.model_dump()[key] != call[key]
            for key in (
                "logical_step_id",
                "call_index",
                "attempt",
                "provider",
                "signature_digest",
                "request_digest",
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对结果的原始意图身份不符")
        gateway = RecordedCallGateway(
            session.history,
            lease.repository_id,
            lease.run_id,
            lambda: (session.head, session.version),
        )
        saved = session.history.objects.get_json(call["result_ref"], ObjectType.CALL)
        outcome = gateway._outcome(occurrence, saved)
        if (
            outcome.error is not None
            or intent.max_response_bytes != session.config.limits.max_response_bytes
            or (
                occurrence.provider.startswith("llm:")
                and intent.model_budget != call_budget(session.config)
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对结果含错误或响应上限改变")
        request = session.history.objects.get(call["request_digest"], ObjectType.BLOB)
        self._materialize(session, call["provider"], request, outcome.payload)
        return CallSettlement(before, request, outcome)

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
