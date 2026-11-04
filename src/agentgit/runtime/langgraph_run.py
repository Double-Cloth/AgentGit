"""固定图的租约保护 Worker；耐久 MCP 结果与原生 pending writes 共同防止重发。"""

import platform
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.adapters.langgraph import LangGraphAdapter
from agentgit.adapters.mcp import MCPAdapter, MCPClient, configured_client
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.langgraph import LangGraphProgram
from agentgit.core.langgraph_runs import LangGraphRunState
from agentgit.core.mcp import MCPBinding
from agentgit.core.models import EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import BudgetUsage
from agentgit.replay.recorded import RecordedAdapter
from agentgit.runtime.langgraph_checkpoints import LangGraphCheckpointJournal
from agentgit.runtime.ports import CallOutcome
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService
from agentgit.services.langgraph_forks import load_fork_source
from agentgit.services.langgraph_recordings import (
    PreparedLangGraphRecording,
    load_recording,
    projection_digest,
)
from agentgit.services.langgraph_runs import LangGraphRunService

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService


class _GraphGateway:
    def __init__(self, owner: "LangGraphRunRunner"):
        self.owner = owner

    def recorded(self, **kwargs: Any) -> CallOutcome | None:
        if self.owner.recording is not None:
            return None
        return self.owner.gateway.recorded(**kwargs)

    def call(self, **kwargs: Any) -> CallOutcome:
        try:
            return self.owner._call(kwargs)
        except AgentGitError as exc:
            # SDK 可能尝试保存 Python Exception；AGJSON 明确拒绝，保留首个实际原因。
            self.owner.call_failure = exc
            raise


class LangGraphRunRunner:
    def __init__(
        self,
        queue: "JobService",
        lease: Lease,
        *,
        fault: Callable[[str], None],
        client_factory: Callable[[MCPBinding], MCPClient] | None = None,
    ):
        self.queue, self.lease, self.fault = queue, lease, fault
        self.client_factory = client_factory or (lambda binding: configured_client(binding.server))

        def guard(connection: Connection, run: Any) -> None:
            queue.guard(connection, lease, run)

        self.history = HistoryService(
            queue.history.engine, queue.history.objects, lease_guard=guard
        )
        self.service = LangGraphRunService(queue)
        self.payload = self.service.payload(lease.repository_id, lease.job_id)
        self.state: LangGraphRunState | None = None
        self.native_raw: bytes | None = None
        self.call_failure: AgentGitError | None = None
        self.native_failure: AgentGitError | None = None
        self.requested_stop: str | None = None
        self.recording: PreparedLangGraphRecording | None = None

    def run(self) -> dict[str, Any]:
        try:
            self._execute()
        except Exception as exc:
            error = (
                self.call_failure
                or self.native_failure
                or (
                    exc
                    if isinstance(exc, AgentGitError)
                    else AgentGitError(ErrorCode.INTERNAL_ERROR, "LangGraph 执行未完成")
                )
            )
            if error.code == ErrorCode.VERSION_CONFLICT:
                return {
                    "job_id": str(self.lease.job_id),
                    "run_id": str(self.lease.run_id),
                    "status": "lease_lost",
                    "error_code": error.code.value,
                }
            if self.requested_stop is not None and self.state is not None:
                self._stop(self.requested_stop)
            elif error.code == ErrorCode.OUTCOME_UNKNOWN and self.state is not None:
                self._save(
                    self.state.model_copy(
                        update={
                            "status": RunStatus.OUTCOME_UNKNOWN,
                            "error_code": error.code.value,
                            "actual_attempts": None,
                        }
                    ),
                    "langgraph.outcome_unknown",
                )
                self.queue.finish(self.lease, "outcome_unknown")
            elif self.state is not None and hasattr(self, "environment"):
                self._save(
                    self.state.model_copy(
                        update={"status": RunStatus.FAILED, "error_code": error.code.value}
                    ),
                    "langgraph.failed",
                )
                self.queue.finish(self.lease, "failed")
            else:
                self._reject(error)
        job = self.queue.get(self.lease.repository_id, self.lease.job_id)
        return {
            "job_id": str(self.lease.job_id),
            "run_id": str(self.lease.run_id),
            "status": job["status"],
            "error_code": job["last_error"],
        }

    def _execute(self) -> None:
        if (
            self.payload.code_revision != source_revision()
            or self.payload.dependency_lock_digest != dependency_lock_digest()
        ):
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph 源码或锁与当前 Worker 不符"
            )
        self.program = self.service.program(self.lease.repository_id, self.payload)
        if self.payload.max_wall_ms != self.program.limits.max_wall_ms:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "LangGraph 墙钟上限与程序不符")
        self.environment = EnvironmentManifest(
            runtime_version=__version__,
            python_version=platform.python_version(),
            code_revision=self.payload.code_revision,
            dependency_lock_digest=self.payload.dependency_lock_digest,
            model_signature="no-model/langgraph-fixed/v1",
            prompt_digest=object_digest(ObjectType.BLOB, b"mcp-read-review-publish/v1"),
            input_ref=self.payload.program_ref,
            evaluation_time="2026-10-20T00:00:00Z",
            random_seed=0,
        )
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            if run["mode"] != ("recorded" if self.payload.replay else "live"):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "LangGraph Job 不能借用其他模式")
            origin_spec = self.payload.fork or self.payload.replay
            origin = UUID(origin_spec.source_run_id) if origin_spec else None
            if job["origin_run_id"] != origin or run["origin_run_id"] != origin:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图 Job 与 Run 来源不符")
            if origin_spec is not None:
                forks = TABLES["branch_forks"]
                branch = (
                    connection.execute(
                        sa.select(forks).where(
                            forks.c.repository_id == self.lease.repository_id,
                            forks.c.branch_name == run["branch_name"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    branch is None
                    or branch["source_run_id"] != origin
                    or branch["from_commit_id"] != origin_spec.from_commit
                ):
                    raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图分支与来源不符")
            self.head = run["checkpoint_commit_id"]
            own = connection.execute(
                sa.select(TABLES["snapshots"].c.commit_id).where(
                    TABLES["snapshots"].c.run_id == self.lease.run_id,
                    TABLES["snapshots"].c.commit_id == self.head,
                )
            ).first()
        self.version = self.history.branch(self.lease.repository_id, run["branch_name"])["version"]
        if self.payload.replay is not None:
            replay_spec = self.payload.replay
            if run["base_commit_id"] != replay_spec.from_commit or run["origin_run_id"] != UUID(
                replay_spec.source_run_id
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图 Recorded Run 来源与载荷不符")
            self.recording = load_recording(self.history, self.lease.repository_id, replay_spec)
            if (
                self.recording.bundle.program_ref != self.payload.program_ref
                or self.recording.program != self.program
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图 Recorded 程序与来源不符")
        fork_source: tuple[LangGraphProgram, LangGraphRunState, bytes] | None = None
        if self.payload.fork is not None:
            spec = self.payload.fork
            if run["base_commit_id"] != spec.from_commit or run["origin_run_id"] != UUID(
                spec.source_run_id
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图 Fork Run 来源与载荷不符")
            fork_source = load_fork_source(
                self.history, self.lease.repository_id, UUID(spec.source_run_id), spec.from_commit
            )
            source_program, source_state, _ = fork_source
            expected_program = source_program.model_copy(
                update={"review_note": self.program.review_note}
            )
            if (
                source_state.program_ref != spec.source_program_ref
                or source_state.native_ref != spec.native_ref
                or self.program != expected_program
            ):
                raise AgentGitError(
                    ErrorCode.REPLAY_MISMATCH, "图 Fork 程序、预算或原生日志来源不符"
                )
        if own is not None:
            self.state = parse_contract(
                encode(self.history.restore(self.lease.repository_id, self.head).data),
                LangGraphRunState,
            )
            if self.state.program_ref != self.payload.program_ref:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "LangGraph 检查点不属于固定程序")
            if self.state.native_ref is not None:
                self.native_raw = self.history.objects.get(self.state.native_ref, ObjectType.BLOB)
                artifacts = self.history.artifacts(self.lease.repository_id, self.head)
                if not any(
                    a["relative_path"] == "langgraph-native.json"
                    and a["object_id"] == self.state.native_ref
                    for a in artifacts
                ):
                    raise AgentGitError(
                        ErrorCode.OBJECT_CORRUPT, "LangGraph 原生记录缺少确切产物引用"
                    )
        elif self.recording is not None:
            source_state = self.recording.before
            if self.recording.native_raw is not None:
                assert self.payload.replay is not None
                self.native_raw = LangGraphCheckpointJournal(
                    self.payload.replay.source_run_id,
                    lambda *args: None,
                    restored=self.recording.native_raw,
                ).clone_thread(str(self.lease.run_id))
            self.state = source_state.model_copy(
                update={
                    "actual_attempts": 0,
                    "native_ref": object_digest(ObjectType.BLOB, self.native_raw)
                    if self.native_raw is not None
                    else None,
                    "replay_control_index": 0,
                    "replay_pause_pending": source_state.status == RunStatus.PAUSED,
                    "replay_call_consumed": source_state.trace is not None,
                }
            )
            self._save(self.state, "langgraph.recorded_initialized")
            self.fault("langgraph.after_recorded_seed")
        elif fork_source is not None:
            assert self.payload.fork is not None
            _, source_state, raw = fork_source
            source_journal = LangGraphCheckpointJournal(
                self.payload.fork.source_run_id, lambda *args: None, restored=raw
            )
            self.native_raw = source_journal.clone_review(str(self.lease.run_id))
            self.state = source_state.model_copy(
                update={
                    "program_ref": self.payload.program_ref,
                    "status": RunStatus.RUNNING,
                    "native_ref": object_digest(ObjectType.BLOB, self.native_raw),
                    "actual_attempts": 0,
                    "review_boundary_reached": True,
                    "fork_initialized": False,
                    "replay_control_index": 0,
                    "replay_pause_pending": False,
                    "replay_call_consumed": False,
                }
            )
            self._save(self.state, "langgraph.fork_seeded")
            self.fault("langgraph.after_fork_seed")
        else:
            self.state = LangGraphRunState(program_ref=self.payload.program_ref)
            self._save(self.state, "langgraph.initialized")
        if fork_source is not None and (
            self.state.actual_attempts != 0
            or self.state.trace != fork_source[1].trace
            or self.state.usage.tool_calls != 1
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Fork 检查点不能伪造新的取证计量或来源")
        if self.recording is not None and (
            self.state.actual_attempts != 0
            or self.state.replay_control_index > len(self.recording.bundle.controls)
            or self.state.replay_call_consumed != (self.state.trace is not None)
            or (self.state.trace is not None and self.state.trace != self.recording.after.trace)
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图回放状态、计量或取证来源不符")
        if self.payload.fork is None and self.recording is None and self.state.fork_initialized:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "普通图任务不能借用 Fork 初始化状态")
        if self.state.status.value in {"completed", "failed", "cancelled", "budget_exhausted"}:
            if self.recording is not None and self.state.status == RunStatus.COMPLETED:
                self._verify_recorded(self.state)
            self.queue.finish(self.lease, self.state.status.value)
            return
        if self.recording is None:
            authorization = ApprovalService(
                self.history,
                ToolPolicy(rules=(self.program.binding.rule(),)),
                actor=self.payload.actor,
            )
            self.gateway = RecordedCallGateway(
                self.history,
                self.lease.repository_id,
                self.lease.run_id,
                lambda: (self.head, self.version),
                authorization=authorization,
                after_result=lambda: self.fault("langgraph.after_result"),
            )
        journal = LangGraphCheckpointJournal(
            str(self.lease.run_id), self._persist_native, restored=self.native_raw
        )
        graph = LangGraphAdapter(
            self.program,
            _GraphGateway(self),
            thread_id=str(self.lease.run_id),
            checkpointer=journal,
            step_boundaries=True,
        )
        while True:
            control = self._control()
            if control in {"pause", "cancel", "budget_exhausted"}:
                self._stop(control)
                return
            if self.recording is not None and self.state.replay_pause_pending:
                self._resume_recorded()
                continue
            if self.state.status != RunStatus.RUNNING:
                self._save(
                    self.state.model_copy(update={"status": RunStatus.RUNNING}), "langgraph.resumed"
                )
            if self._recorded_ready("requested"):
                self._pause_recorded("requested")
                continue
            if (
                self.payload.fork is not None
                or (self.recording is not None and self.recording.bundle.needs_fork_initialization)
            ) and not self.state.fork_initialized:
                graph.update_note(self.program.review_note)
                if not self.state.fork_initialized:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "图 Fork 原生备注更新未完成")
                self.fault("langgraph.after_fork_initialized")
                continue
            if (
                self.state.native_operation == "checkpoint"
                and self.state.native_next == ("review",)
                and self.program.graph.pause_before
                and not self.state.review_boundary_reached
            ):
                if self.recording is not None:
                    self._pause_recorded("native")
                    continue
                self._save(
                    self.state.model_copy(
                        update={"status": RunStatus.PAUSED, "review_boundary_reached": True}
                    ),
                    "langgraph.native_paused",
                )
                self.queue.finish(self.lease, "paused")
                return
            if (
                self.native_raw is not None
                and self.state.native_operation == "checkpoint"
                and not self.state.native_next
                and self.state.values
            ):
                code = self.state.values.get("error_code")
                finished = self.state.model_copy(
                    update={
                        "status": RunStatus.FAILED if code else RunStatus.COMPLETED,
                        "error_code": code,
                    }
                )
                if self.recording is not None:
                    self._verify_recorded(finished)
                self._save(finished, "langgraph.finished")
                self.queue.finish(self.lease, self.state.status.value)
                return
            graph.invoke(start=self.native_raw is None)

    def _call(self, kwargs: dict[str, Any]) -> CallOutcome:
        assert self.state is not None
        if self.payload.fork is not None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "图 Fork 不得重新执行已保存的取证节点")
        binding = self.program.binding
        request = encode(self.program.request.model_dump(mode="json"))
        if (
            kwargs["step"] != "fetch"
            or kwargs["attempt"] != 1
            or kwargs["provider"] != binding.provider
            or kwargs["signature"] != binding.signature()
            or kwargs["effect"] != binding.effect_class
            or kwargs["request"] != request
            or kwargs["max_bytes"] != binding.server.max_response_bytes
            or kwargs.get("model_budget") is not None
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "LangGraph 调用不匹配固定取证节点")
        identity = {key: value for key, value in kwargs.items() if key != "invoke"}
        if self.recording is not None:
            return self._recorded_call(kwargs)
        outcome = self.gateway.recorded(**identity)
        if outcome is None:
            control = self._control()
            if control in {"pause", "cancel", "budget_exhausted"}:
                self.requested_stop = control
                raise AgentGitError(ErrorCode.FORBIDDEN, "LangGraph 取证前已请求控制")
            if not self.state.pending:
                self._save(
                    self.state.model_copy(
                        update={
                            "pending": True,
                            "actual_attempts": None,
                            "usage": BudgetUsage(steps=1, tool_calls=1),
                        }
                    ),
                    "langgraph.fetch_prepared",
                )
            if self.state.usage.tool_calls != 1 or self.state.usage.steps != 1:
                raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "LangGraph 取证不能重复预留调用")
            client = self.client_factory(binding)
            with self.history.engine.begin() as connection:
                job = self.queue.guard(connection, self.lease, self.lease.run_id)
                now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
                remaining = int((job["deadline_at"] - now).total_seconds() * 1000)
            if remaining <= 0:
                self.requested_stop = "budget_exhausted"
                raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "LangGraph 取证期限已到")
            client.restrict_deadline(remaining)
            client.guard_requests(self._request_guard)

            def invoke() -> bytes:
                self.fault("langgraph.after_intent")
                return MCPAdapter(binding, client).invoke(request)

            outcome = self.gateway.call(**identity, invoke=invoke)
        self.state = self.state.model_copy(
            update={"pending": False, "actual_attempts": 1, "trace": outcome.trace}
        )
        return outcome

    def _recorded_ready(self, kind: str) -> bool:
        if self.recording is None or self.state is None:
            return False
        index = self.state.replay_control_index
        controls = self.recording.bundle.controls
        return (
            index < len(controls)
            and controls[index].kind == kind
            and controls[index].before_digest == projection_digest(self.state)
        )

    def _pause_recorded(self, kind: str) -> None:
        assert self.recording is not None and self.state is not None
        if not self._recorded_ready(kind):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图缺少当前暂停的录制依据")
        index = self.state.replay_control_index
        paused = self.state.model_copy(
            update={
                "status": RunStatus.PAUSED,
                "replay_pause_pending": True,
                "review_boundary_reached": self.state.review_boundary_reached or kind == "native",
            }
        )
        if projection_digest(paused) != self.recording.bundle.controls[index].paused_digest:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图录制暂停投影不符")
        self._save(
            paused,
            "langgraph.native_paused" if kind == "native" else "langgraph.control_applied",
            recorded_control=index,
        )
        self.fault("langgraph.after_recorded_pause")

    def _resume_recorded(self) -> None:
        assert self.recording is not None and self.state is not None
        index = self.state.replay_control_index
        if index >= len(self.recording.bundle.controls):
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "图暂停缺少恢复录制")
        control = self.recording.bundle.controls[index]
        if projection_digest(self.state) != control.paused_digest:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图录制恢复起点不符")
        resumed = self.state.model_copy(
            update={
                "status": RunStatus.RUNNING,
                "replay_pause_pending": False,
                "replay_control_index": index + 1,
            }
        )
        if projection_digest(resumed) != control.resumed_digest:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图录制恢复投影不符")
        self._save(resumed, "langgraph.resumed", recorded_control=index)

    def _recorded_call(self, kwargs: dict[str, Any]) -> CallOutcome:
        assert self.recording is not None and self.state is not None
        if self.state.replay_call_consumed or not self.recording.bundle.needs_fetch:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图回放不能重复消费取证调用")
        control = self._control()
        if control in {"pause", "cancel", "budget_exhausted"}:
            self.requested_stop = control
            raise AgentGitError(ErrorCode.FORBIDDEN, "图回放取证前已请求控制")
        if self._recorded_ready("requested"):
            self._pause_recorded("requested")
            self._resume_recorded()
        if not self.state.pending:
            self._save(
                self.state.model_copy(
                    update={
                        "pending": True,
                        "actual_attempts": 0,
                        "usage": BudgetUsage(steps=1, tool_calls=1),
                    }
                ),
                "langgraph.fetch_prepared",
            )
        if self._recorded_ready("requested"):
            self._pause_recorded("requested")
            self._resume_recorded()
        if self.state.usage.tool_calls != 1 or self.state.usage.steps != 1:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图回放取证预留不符")
        record = self.recording.record
        adapter = RecordedAdapter((record,), lambda: record.intent.checkpoint)
        outcome = adapter.call(**kwargs)
        adapter.assert_consumed()
        self.state = self.state.model_copy(
            update={
                "pending": False,
                "actual_attempts": 0,
                "trace": outcome.trace,
                "replay_call_consumed": True,
            }
        )
        self.fault("langgraph.after_recorded_call")
        return outcome

    def _verify_recorded(self, state: LangGraphRunState) -> None:
        assert self.recording is not None
        if (
            state.logical_projection() != self.recording.after.logical_projection()
            or state.trace != self.recording.after.trace
            or state.actual_attempts != 0
            or not state.replay_call_consumed
            or state.replay_pause_pending
            or state.replay_control_index != len(self.recording.bundle.controls)
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图回放终点、控制消费或报告与来源不符")

    def _persist_native(self, raw: bytes, kind: str, native_id: str) -> None:
        try:
            self._write_native(raw, kind, native_id)
        except Exception as exc:
            if self.native_failure is None:
                self.native_failure = (
                    exc
                    if isinstance(exc, AgentGitError)
                    else AgentGitError(ErrorCode.INTERNAL_ERROR, "LangGraph 原生保存未完成")
                )
            raise

    def _write_native(self, raw: bytes, kind: str, native_id: str) -> None:
        assert self.state is not None
        # 使用独立候选索引读取 SDK 投影；不可在尚未确认的原实例上递归查询。
        journal = LangGraphCheckpointJournal(
            str(self.lease.run_id), lambda *args: None, restored=raw
        )
        candidate = LangGraphAdapter(
            self.program,
            _GraphGateway(self),
            thread_id=str(self.lease.run_id),
            checkpointer=journal,
            step_boundaries=True,
        ).state()
        values = dict(candidate.values)
        usage = self.state.usage.model_copy(
            update={"steps": max(self.state.usage.steps, len(values.get("node_order", [])))}
        )
        state = self.state.model_copy(
            update={
                "native_ref": object_digest(ObjectType.BLOB, raw),
                "native_operation": kind,
                "native_next": tuple(candidate.next),
                "values": values,
                "usage": usage,
                "fork_initialized": self.state.fork_initialized
                or (
                    (
                        self.payload.fork is not None
                        or (
                            self.recording is not None
                            and self.recording.bundle.needs_fork_initialization
                        )
                    )
                    and kind == "checkpoint"
                    and decode(raw)["operations"][-1].get("metadata", {}).get("source") == "update"
                    and values.get("review_note") == self.program.review_note
                    and values.get("node_order") == ["fetch"]
                    and tuple(candidate.next) == ("review",)
                ),
            }
        )
        self._save(state, "langgraph.native." + kind, native_raw=raw, native_id=native_id)
        self.fault("langgraph.after_" + kind)
        if values.get("node_order") == ["fetch"]:
            self.fault("langgraph.after_fetch_" + kind)
            if self.payload.fork is not None:
                self.fault("langgraph.after_fork_" + kind)
        if self.recording is not None:
            self.fault("langgraph.after_recorded_" + kind)

    def _control(self) -> str | None:
        action = self.queue.apply_control(self.lease)
        if action in {"pause", "cancel"}:
            return action
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            return "budget_exhausted" if job["deadline_at"] <= now else action

    def _request_guard(self) -> None:
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)

    def _stop(self, control: str) -> None:
        assert self.state is not None
        status = {
            "pause": RunStatus.PAUSED,
            "cancel": RunStatus.CANCELLED,
            "budget_exhausted": RunStatus.BUDGET_EXHAUSTED,
        }[control]
        self._save(
            self.state.model_copy(
                update={
                    "status": status,
                    "error_code": ErrorCode.BUDGET_EXHAUSTED.value
                    if control == "budget_exhausted"
                    else self.state.error_code,
                }
            ),
            "langgraph.control_applied",
        )
        self.queue.finish(self.lease, status.value)

    def _save(
        self,
        state: LangGraphRunState,
        event: str,
        *,
        native_raw: bytes | None = None,
        native_id: str | None = None,
        recorded_control: int | None = None,
    ) -> None:
        state = parse_contract(encode(state.model_dump(mode="json")), LangGraphRunState)
        raw = native_raw if native_raw is not None else self.native_raw
        artifacts = []
        if raw is not None:
            if state.native_ref != object_digest(ObjectType.BLOB, raw):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "LangGraph 原生记录与状态引用不符")
            artifacts.append(ArtifactInput("langgraph-native.json", raw, "application/json"))
        if state.values.get("report"):
            artifacts.append(
                ArtifactInput("report.md", state.values["report"].encode(), "text/markdown")
            )
        if (
            len(artifacts) > self.program.limits.max_files
            or sum(len(a.content) for a in artifacts) > self.program.limits.max_artifact_bytes
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "LangGraph 原生记录与产物超过共同预算")

        def projection(connection: Connection) -> None:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(status=state.status.value, failure_code=state.error_code)
            )
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            if (
                recorded_control is None
                and event != "langgraph.recorded_initialized"
                and state.status in {RunStatus.PAUSED, RunStatus.CANCELLED}
            ) or (run["control_status"] == "requested" and run["control_request"] == "resume"):
                self.queue.mark_control_applied(connection, self.lease)

        result = self.history.commit(
            self.lease.repository_id,
            self.lease.run_id,
            expected_head=self.head,
            expected_version=self.version,
            state=state.model_dump(mode="json"),
            environment=self.environment,
            events=(
                EventInput(
                    event,
                    "graph",
                    {
                        "program_ref": state.program_ref,
                        "native_ref": state.native_ref,
                        "native_checkpoint_id": native_id,
                        "error_code": state.error_code,
                        **(
                            {"recorded_source": self.payload.replay.model_dump(mode="json")}
                            if self.payload.replay
                            else {}
                        ),
                        **(
                            {
                                "recorded_control": {
                                    "bundle_ref": self.payload.replay.bundle_ref,
                                    "index": recorded_control,
                                }
                            }
                            if recorded_control is not None and self.payload.replay
                            else {}
                        ),
                        **(
                            {"fork_source": self.payload.fork.model_dump(mode="json")}
                            if self.payload.fork is not None
                            else {}
                        ),
                    },
                ),
            ),
            artifacts=tuple(artifacts),
            projection=projection,
        )
        self.head, self.version, self.state = result.commit_id, result.branch_version, state
        self.native_raw = raw

    def _reject(self, error: AgentGitError) -> None:
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            obj = self.history.objects.put_json(
                ObjectType.EVENT, {"schema_version": 1, "data": {"error_code": error.code.value}}
            )
            self.history._event(
                connection,
                self.lease.repository_id,
                self.lease.run_id,
                run["last_event_seq"] + 1,
                EventInput("langgraph.rejected", "graph", {"error_code": error.code.value}),
                obj,
            )
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(
                    status="failed",
                    failure_code=error.code.value,
                    last_event_seq=run["last_event_seq"] + 1,
                )
            )
        self.queue.finish(self.lease, "failed")
