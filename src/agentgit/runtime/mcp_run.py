"""一次 MCP 调用的租约保护状态机；耐久结果先于控制和新请求消费。"""

import platform
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.adapters.mcp import MCPAdapter, MCPClient, configured_client
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.mcp import MCPBinding
from agentgit.core.mcp_runs import MCPRunState
from agentgit.core.models import EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import BudgetUsage
from agentgit.replay.recorded import RecordedAdapter
from agentgit.runtime.ports import CallOutcome
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService
from agentgit.services.mcp_runs import MCPRunService

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService


def worker_client(binding: MCPBinding) -> MCPClient:
    return configured_client(binding.server)


class MCPRunRunner:
    def __init__(
        self,
        queue: "JobService",
        lease: Lease,
        *,
        fault: Callable[[str], None],
        client_factory: Callable[[MCPBinding], MCPClient] | None = None,
    ):
        self.queue, self.lease, self.fault = queue, lease, fault
        self.client_factory = client_factory or worker_client

        def guard(connection: Connection, run_id: UUID) -> None:
            queue.guard(connection, lease, run_id)

        self.history = HistoryService(
            queue.history.engine,
            queue.history.objects,
            lease_guard=guard,
        )
        self.service = MCPRunService(queue)
        self.payload = self.service.payload(lease.repository_id, lease.job_id)
        self.state: MCPRunState | None = None

    def run(self) -> dict[str, Any]:
        try:
            self._execute()
        except AgentGitError as exc:
            if exc.code == ErrorCode.VERSION_CONFLICT:
                return {
                    "job_id": str(self.lease.job_id),
                    "run_id": str(self.lease.run_id),
                    "status": "lease_lost",
                    "error_code": exc.code.value,
                }
            if exc.code != ErrorCode.APPROVAL_REQUIRED:
                self._reject(exc)
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
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "MCP Job 源码或锁与当前 Worker 不符")
        self.program = self.service.program(self.lease.repository_id, self.payload)
        if self.payload.max_wall_ms != self.program.limits.max_wall_ms:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "MCP Job 墙钟上限与固定程序不符")
        self.environment = EnvironmentManifest(
            runtime_version=__version__,
            python_version=platform.python_version(),
            code_revision=self.payload.code_revision,
            dependency_lock_digest=self.payload.dependency_lock_digest,
            model_signature="no-model/mcp-single-call/v1",
            prompt_digest=object_digest(ObjectType.BLOB, b"mcp-single-call/v1"),
            input_ref=self.payload.program_ref,
            evaluation_time="2026-10-20T00:00:00Z",
            random_seed=0,
        )
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            if run["mode"] != ("recorded" if self.payload.replay is not None else "live"):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "MCP Run 模式与固定 Job 不符")
            self.head = run["checkpoint_commit_id"]
            own = connection.execute(
                sa.select(TABLES["snapshots"].c.commit_id).where(
                    TABLES["snapshots"].c.run_id == self.lease.run_id,
                    TABLES["snapshots"].c.commit_id == self.head,
                )
            ).first()
        self.version = self.history.branch(self.lease.repository_id, run["branch_name"])["version"]
        if own is not None or self.payload.replay is not None:
            self.state = parse_contract(
                encode(self.history.restore(self.lease.repository_id, self.head).data), MCPRunState
            )
            if self.state.program_ref != self.payload.program_ref:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "MCP 检查点与固定程序不符")
        else:
            self.state = MCPRunState(program_ref=self.payload.program_ref)
            control = self._control()
            if control in {"pause", "cancel", "budget_exhausted"}:
                self._stop(control)
                return
            self._save(self.state, "mcp.initialized")
        if self.state.status.value in {"completed", "failed", "cancelled", "budget_exhausted"}:
            self._verify_replay(self.state)
            self.queue.finish(self.lease, self.state.status.value)
            return
        if self.state.response_ref is not None:
            self._finish_observation(None)
            return
        binding = self.program.binding
        request = encode(self.program.request.model_dump(mode="json"))
        if self.payload.replay is not None:
            control = self._control()
            if control in {"pause", "cancel", "budget_exhausted"}:
                self.state = self.state.model_copy(update={"actual_attempts": 0})
                self._stop(control)
                return
            record = self.service.recording(
                self.lease.repository_id, self.payload.program_ref, self.payload.replay
            )
            replay = RecordedAdapter((record,), lambda: record.intent.checkpoint)
            recorded_outcome = MCPAdapter(binding).call(
                replay, step="mcp-tool", attempt=1, arguments=self.program.request.arguments
            )
            replay.assert_consumed()
            self.state = self.state.model_copy(update={"actual_attempts": 0})
            self._finish_observation(recorded_outcome)
            return
        authorization = ApprovalService(
            self.history, ToolPolicy(rules=(binding.rule(),)), actor=self.payload.actor
        )
        gateway = RecordedCallGateway(
            self.history,
            self.lease.repository_id,
            self.lease.run_id,
            lambda: (self.head, self.version),
            authorization=authorization,
            after_result=lambda: self.fault("mcp.after_result"),
        )
        try:
            outcome = gateway.recorded(
                step="mcp-tool",
                attempt=1,
                provider=binding.provider,
                signature=binding.signature(),
                effect=binding.effect_class,
                request=request,
                max_bytes=binding.server.max_response_bytes,
            )
        except AgentGitError as exc:
            if exc.code != ErrorCode.OUTCOME_UNKNOWN:
                raise
            # 网关已核对原意图/策略后才报告未知；保持待核对，不降为普通失败。
            state = self.state.model_copy(
                update={
                    "status": RunStatus.OUTCOME_UNKNOWN,
                    "error_code": ErrorCode.OUTCOME_UNKNOWN.value,
                    "actual_attempts": None,
                }
            )
            self._save(state, "mcp.outcome_unknown")
            self.queue.finish(self.lease, "outcome_unknown")
            return
        if outcome is not None:
            self._finish_observation(outcome)
            return
        control = self._control()
        if control in {"pause", "cancel", "budget_exhausted"}:
            self._stop(control)
            return
        if not self.state.pending:
            self._save(
                self.state.model_copy(
                    update={
                        "status": RunStatus.RUNNING,
                        "pending": True,
                        "usage": BudgetUsage(steps=1, tool_calls=1),
                        "actual_attempts": None,
                    }
                ),
                "mcp.tool_prepared",
            )
        elif self.state.status != RunStatus.RUNNING:
            self._save(self.state.model_copy(update={"status": RunStatus.RUNNING}), "mcp.resumed")
        if self.state.usage.tool_calls != 1 or self.state.usage.steps != 1:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "MCP 调用次数或状态与单次预算不符")
        # 客户端仅在没有完整结果/未知意图且控制允许后创建；Recorded 不走这里。
        client = self.client_factory(binding)
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            remaining = int((job["deadline_at"] - now).total_seconds() * 1000)
        if remaining <= 0:
            self._stop("budget_exhausted")
            return
        client.restrict_deadline(remaining)
        client.guard_requests(self._request_guard)
        adapter = MCPAdapter(binding, client)

        def invoke() -> bytes:
            self.fault("mcp.after_intent")
            return adapter.invoke(request)

        outcome = gateway.call(
            step="mcp-tool",
            attempt=1,
            provider=binding.provider,
            signature=binding.signature(),
            effect=binding.effect_class,
            request=request,
            max_bytes=binding.server.max_response_bytes,
            invoke=invoke,
        )
        self._finish_observation(outcome)

    def _finish_observation(self, outcome: CallOutcome | None) -> None:
        assert self.state is not None
        state = self.state
        if outcome is not None:
            if outcome.error is not None:
                status = (
                    RunStatus.OUTCOME_UNKNOWN
                    if outcome.error.code == ErrorCode.OUTCOME_UNKNOWN
                    else RunStatus.FAILED
                )
                state = state.model_copy(
                    update={
                        "status": status,
                        "pending": status == RunStatus.OUTCOME_UNKNOWN,
                        "trace": outcome.trace,
                        "error_code": outcome.error.code.value,
                        "actual_attempts": None if status == RunStatus.OUTCOME_UNKNOWN else 1,
                    }
                )
            else:
                state = state.model_copy(
                    update={
                        "status": RunStatus.COMPLETED,
                        "pending": False,
                        "response_ref": outcome.trace.response_ref,
                        "trace": outcome.trace,
                        "error_code": None,
                        "actual_attempts": 0 if self.payload.replay is not None else 1,
                    }
                )
        elif state.response_ref is not None:
            state = state.model_copy(update={"status": RunStatus.COMPLETED})
        control = self._control()
        if state.status != RunStatus.OUTCOME_UNKNOWN and control in {
            "pause",
            "cancel",
            "budget_exhausted",
        }:
            state = state.model_copy(
                update={
                    "status": self._control_status(control),
                    "error_code": ErrorCode.BUDGET_EXHAUSTED.value
                    if control == "budget_exhausted"
                    else state.error_code,
                }
            )
        self._verify_replay(state)
        self._save(state, "mcp.recorded" if self.payload.replay is not None else "mcp.observed")
        self.fault("mcp.after_observation")
        self.queue.finish(self.lease, state.status.value)

    def _verify_replay(self, state: MCPRunState) -> None:
        if self.payload.replay is None or state.status != RunStatus.COMPLETED:
            return
        expected = parse_contract(
            encode(
                self.history.restore(self.lease.repository_id, self.payload.replay.to_commit).data
            ),
            MCPRunState,
        )
        if state.logical_projection() != expected.logical_projection():
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "MCP Recorded 最终逻辑投影与来源不符")

    @staticmethod
    def _control_status(control: str) -> RunStatus:
        return {
            "pause": RunStatus.PAUSED,
            "cancel": RunStatus.CANCELLED,
            "budget_exhausted": RunStatus.BUDGET_EXHAUSTED,
        }[control]

    def _control(self) -> str | None:
        action = self.queue.apply_control(self.lease)
        if action in {"pause", "cancel"}:
            return action
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            if job["deadline_at"] <= now:
                return "budget_exhausted"
        return action

    def _request_guard(self) -> None:
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)

    def _stop(self, control: str) -> None:
        assert self.state is not None
        self._save(
            self.state.model_copy(
                update={
                    "status": self._control_status(control),
                    "error_code": ErrorCode.BUDGET_EXHAUSTED.value
                    if control == "budget_exhausted"
                    else self.state.error_code,
                }
            ),
            "mcp.control_applied",
        )
        self.queue.finish(self.lease, self.state.status.value)

    def _save(self, state: MCPRunState, event: str) -> None:
        state = parse_contract(encode(state.model_dump(mode="json")), MCPRunState)
        artifacts: tuple[ArtifactInput, ...] = ()
        if state.response_ref is not None:
            content = self.history.objects.get(state.response_ref, ObjectType.BLOB)
            if len(content) > self.program.limits.max_artifact_bytes:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 原始产物超过程序预算")
            artifacts = (ArtifactInput("mcp-result.json", content, "application/json"),)

        def projection(connection: Connection) -> None:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(status=state.status.value, failure_code=state.error_code)
            )
            if state.status in {RunStatus.PAUSED, RunStatus.CANCELLED}:
                self.queue.mark_control_applied(connection, self.lease)
            else:
                run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
                if run["control_status"] == "requested" and run["control_request"] == "resume":
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
                    "mcp-tool",
                    {
                        "program_ref": state.program_ref,
                        "response_ref": state.response_ref,
                        "error_code": state.error_code,
                    },
                ),
            ),
            artifacts=artifacts,
            projection=projection,
        )
        self.head, self.version, self.state = result.commit_id, result.branch_version, state
        self.fault("mcp.after_checkpoint")

    def _reject(self, error: AgentGitError) -> None:
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            payload = self.history.objects.put_json(
                ObjectType.EVENT, {"schema_version": 1, "data": {"error_code": error.code.value}}
            )
            event = EventInput("mcp.rejected", "mcp-tool", {"error_code": error.code.value})
            self.history._event(
                connection,
                self.lease.repository_id,
                self.lease.run_id,
                run["last_event_seq"] + 1,
                event,
                payload,
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
