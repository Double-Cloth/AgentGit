"""A2A 单次领取推进一项协议动作；检查点先于调度，未知写不重发。"""

import platform
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.adapters.a2a import A2AAdapter, A2AClient, configured_client
from agentgit.core.a2a import A2ABinding
from agentgit.core.a2a_runs import A2AObservation, A2ARetiredCall, A2ARunState
from agentgit.core.a2a_wire import A2ACall, A2AResult
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import BudgetUsage
from agentgit.runtime.ports import CallOutcome
from agentgit.services.a2a_runs import A2ARunService, check_program_secrets
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService

REMOTE_TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_CANCELED",
}
STOPPED = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.BUDGET_EXHAUSTED,
    RunStatus.OUTCOME_UNKNOWN,
}


def worker_client(binding: A2ABinding) -> A2AClient:
    return configured_client(binding.server)


class A2ARunRunner:
    def __init__(
        self,
        queue: "JobService",
        lease: Lease,
        *,
        fault: Callable[[str], None],
        client_factory: Callable[[A2ABinding], A2AClient] | None = None,
    ):
        self.queue, self.lease, self.fault = queue, lease, fault
        self.client_factory = client_factory or worker_client

        def guard(connection: Connection, run_id: UUID) -> None:
            queue.guard(connection, lease, run_id)

        self.history = HistoryService(
            queue.history.engine, queue.history.objects, lease_guard=guard
        )
        self.service = A2ARunService(queue)
        self.payload = self.service.payload(lease.repository_id, lease.job_id)

    def run(self) -> dict[str, Any]:
        try:
            self._execute()
        except AgentGitError as error:
            if error.code == ErrorCode.VERSION_CONFLICT:
                return dict(
                    job_id=str(self.lease.job_id),
                    run_id=str(self.lease.run_id),
                    status="lease_lost",
                    error_code=error.code.value,
                )
            if error.code == ErrorCode.CONTROL_REQUESTED:
                if not self._control(self.queue.apply_control(self.lease)):
                    self._release()
            elif error.code != ErrorCode.APPROVAL_REQUIRED:
                self._reject(error)
        job = self.queue.get(self.lease.repository_id, self.lease.job_id)
        return dict(
            job_id=str(self.lease.job_id),
            run_id=str(self.lease.run_id),
            status=job["status"],
            error_code=job["last_error"],
        )

    def _execute(self) -> None:
        if (
            self.payload.code_revision != source_revision()
            or self.payload.dependency_lock_digest != dependency_lock_digest()
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "A2A Job 源码或锁与 Worker 不符")
        self.program = self.service.program(self.lease.repository_id, self.payload)
        check_program_secrets(self.program)
        if self.payload.max_wall_ms != self.program.limits.max_wall_ms:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A Job 与固定墙钟预算不符")
        self.environment = EnvironmentManifest(
            runtime_version=__version__,
            python_version=platform.python_version(),
            code_revision=self.payload.code_revision,
            dependency_lock_digest=self.payload.dependency_lock_digest,
            model_signature="no-model/fixed-a2a/v1",
            prompt_digest=object_digest(ObjectType.BLOB, b"fixed-a2a/v1"),
            input_ref=self.payload.program_ref,
            evaluation_time="2026-10-31T00:00:00Z",
            random_seed=0,
        )
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
        if run["mode"] != "live":
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "A2A 当前耐久入口只执行声明的 Live 模式"
            )
        self.head = run["checkpoint_commit_id"]
        self.version = self.history.branch(self.lease.repository_id, run["branch_name"])["version"]
        restored = self.service.state(
            self.lease.repository_id, self.lease.run_id, actor=self.payload.actor
        )
        self.state = restored or A2ARunState(program_ref=self.payload.program_ref)
        if (
            self.state.usage.tool_calls > self.program.limits.max_tool_calls
            or self.state.polls > self.program.max_polls
            or self.state.next_message > len(self.program.messages)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 检查点超出固定预算或消息范围")
        self._last_result()
        if self.state.status in STOPPED:
            self.queue.finish(self.lease, self.state.status.value)
            return
        if restored is None:
            self._save(self.state, "a2a.initialized")
        self.authorization = ApprovalService(
            self.history,
            ToolPolicy(
                rules=(
                    self.program.binding.rule("send"),
                    self.program.binding.rule("get"),
                    self.program.binding.rule("cancel"),
                )
            ),
            actor=self.payload.actor,
        )
        self.gateway = RecordedCallGateway(
            self.history,
            self.lease.repository_id,
            self.lease.run_id,
            lambda: (self.head, self.version),
            authorization=self.authorization,
            after_result=lambda: self.fault("a2a.after_result"),
        )
        # 先消费确切结果或识别未知意图，不能用新控制越过在途效果。
        if self.state.pending is not None:
            call = self.state.pending
            self._validate_reference(call)
            try:
                cached = self.gateway.recorded(
                    step=self.state.pending_step,
                    attempt=1,
                    provider=self.program.binding.provider(call.operation),
                    signature=self.program.binding.signature(call.operation),
                    effect=self.program.binding.effect(call.operation),
                    request=encode(call.model_dump(mode="json")),
                    max_bytes=self.program.binding.server.max_response_bytes,
                )
            except AgentGitError as error:
                if error.code != ErrorCode.OUTCOME_UNKNOWN:
                    raise
                self._unknown()
                return
            if cached is not None:
                self._observe(cached)
                return
        action = self.queue.apply_control(self.lease)
        if self._control(action):
            return
        if self.state.status == RunStatus.PAUSED:
            if action != "resume":
                self.queue.finish(self.lease, "paused")
                return
            self._save(
                self.state.model_copy(
                    update=dict(
                        status=RunStatus.RUNNING,
                        waiting_for=None,
                        defer_pending=False,
                        error_code=None,
                    )
                ),
                "a2a.resumed",
                retire_approvals=True,
            )
        if self.state.defer_pending and self.lease.recovery_required:
            self._release()
            return
        if self.state.pending is None and self.state.remote_state in REMOTE_TERMINAL:
            status = {
                "TASK_STATE_COMPLETED": RunStatus.COMPLETED,
                "TASK_STATE_CANCELED": RunStatus.CANCELLED,
            }.get(self.state.remote_state, RunStatus.FAILED)
            self._save(
                self.state.model_copy(
                    update=dict(
                        status=status,
                        defer_pending=False,
                        error_code=ErrorCode.VALIDATION_FAILED.value
                        if status == RunStatus.FAILED
                        else None,
                    )
                ),
                "a2a.known_terminal",
            )
            self.queue.finish(self.lease, status.value)
            return
        if self.state.pending is None:
            next_call = self._next_call()
            if next_call is None:
                return
            call = next_call
            if self.state.usage.tool_calls >= self.program.limits.max_tool_calls:
                self._stop("budget_exhausted")
                return
            usage = BudgetUsage(
                steps=self.state.usage.steps + 1, tool_calls=self.state.usage.tool_calls + 1
            )
            self._save(
                self.state.model_copy(
                    update=dict(
                        status=RunStatus.RUNNING,
                        pending=call,
                        defer_pending=False,
                        usage=usage,
                        polls=self.state.polls + (call.operation == "get"),
                        waiting_for=None,
                        error_code=None,
                    )
                ),
                "a2a.call_prepared",
            )
            self.fault("a2a.after_prepare")
        if self._control(self.queue.apply_control(self.lease)):
            return
        assert self.state.pending is not None
        call = self.state.pending
        self._validate_reference(call)
        client = self.client_factory(self.program.binding)
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            remaining = int((job["deadline_at"] - now).total_seconds() * 1000)
        if remaining <= 0:
            self._stop("budget_exhausted")
            return
        client.restrict_deadline(remaining)
        client.guard_requests(self._request_guard)
        adapter = A2AAdapter(self.program.binding, client)
        raw = encode(call.model_dump(mode="json"))

        def invoke() -> bytes:
            self.fault("a2a.after_intent")
            return adapter.invoke(raw)

        outcome = self.gateway.call(
            step=self.state.pending_step,
            attempt=1,
            provider=self.program.binding.provider(call.operation),
            signature=self.program.binding.signature(call.operation),
            effect=self.program.binding.effect(call.operation),
            request=raw,
            invoke=invoke,
            max_bytes=self.program.binding.server.max_response_bytes,
        )
        self._observe(outcome)

    def _last_result(self) -> A2AResult | None:
        if self.state.last_response_ref is None:
            return None
        result = self.service.confirmed_result(
            self.lease.repository_id, self.lease.run_id, self.state.last_response_ref, self.program
        )
        if result.value()["status"]["state"] != self.state.remote_state:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 本地状态与已确认远端回执不符")
        return result

    def _validate_reference(self, call: A2ACall) -> None:
        try:
            call.validate_binding(self.program.binding)
        except ValueError:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 未决调用绑定不符") from None
        if call.task is not None:
            result = self.service.confirmed_result(
                self.lease.repository_id, self.lease.run_id, call.task.response_ref, self.program
            )
            if result.reference() != call.task:
                raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 引用不是本 Run 的确切已确认回执")

    def _next_call(self) -> A2ACall | None:
        result = self._last_result()
        if result is None or (
            self.state.remote_state == "TASK_STATE_INPUT_REQUIRED"
            and self.state.next_message < len(self.program.messages)
            and not self.state.cancel_requested
        ):
            raw = self.program.messages[self.state.next_message]
            body = A2ACall(operation="send", body_json=raw).body()
            reference = result.reference() if result else None
            if reference is not None:
                body["message"].update(taskId=reference.task_id, contextId=reference.context_id)
            return A2ACall(operation="send", body_json=encode(body).decode(), task=reference)
        reference = result.reference()
        if self.state.cancel_requested:
            return A2ACall(
                operation="cancel",
                body_json=encode({"id": reference.task_id}).decode(),
                task=reference,
            )
        if self.state.polls >= self.program.max_polls:
            self._stop("budget_exhausted")
            return None
        return A2ACall(
            operation="get", body_json=encode({"id": reference.task_id}).decode(), task=reference
        )

    def _control(self, action: str | None) -> bool:
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            expired = job["deadline_at"] <= now
        if expired:
            if action == "cancel":
                self.state = self.state.model_copy(
                    update=dict(cancel_requested=True, remote_cancel_status="not_sent")
                )
            action = "budget_exhausted"
        if action in ("pause", "budget_exhausted"):
            self._stop(action)
            return True
        if action != "cancel":
            return False
        if self.state.pending is not None and self.state.pending.operation == "cancel":
            with self.history.engine.begin() as connection:
                self.queue.mark_control_applied(connection, self.lease)
            return False
        if self.state.last_response_ref is None or self.state.remote_state in REMOTE_TERMINAL:
            self._stop("cancel")
            return True
        if not self.state.cancel_requested or self.state.pending is not None:
            state = self._retire_pending().model_copy(
                update=dict(
                    cancel_requested=True,
                    remote_cancel_status="pending_approval",
                    defer_pending=False,
                    status=RunStatus.RUNNING,
                    waiting_for=None,
                )
            )
            self._save(state, "a2a.cancel_requested", retire_approvals=True)
        return False

    def _retire_pending(self) -> A2ARunState:
        if self.state.pending is None:
            return self.state
        retired = A2ARetiredCall(step=self.state.pending_step, call=self.state.pending)
        return self.state.model_copy(
            update=dict(pending=None, retired=(*self.state.retired, retired))
        )

    def _stop(self, action: str) -> None:
        state = self._retire_pending() if action in ("cancel", "budget_exhausted") else self.state
        changes: dict[str, Any] = dict(
            status={
                "pause": RunStatus.PAUSED,
                "cancel": RunStatus.CANCELLED,
                "budget_exhausted": RunStatus.BUDGET_EXHAUSTED,
            }[action],
            defer_pending=False,
        )
        if action == "cancel":
            changes.update(cancel_requested=True, remote_cancel_status="not_sent")
        if action == "budget_exhausted":
            changes["error_code"] = ErrorCode.BUDGET_EXHAUSTED.value
            if state.cancel_requested:
                changes["remote_cancel_status"] = "not_sent"
        self._save(state.model_copy(update=changes), "a2a.control_applied", retire_approvals=True)
        self.queue.finish(self.lease, self.state.status.value)

    def _request_guard(self) -> None:
        action = self.queue.apply_control(self.lease)
        if action in ("pause", "budget_exhausted") or (
            action == "cancel"
            and self.state.pending is not None
            and self.state.pending.operation != "cancel"
        ):
            raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "A2A 新请求前收到停止控制")

    def _unknown(self) -> None:
        changes: dict[str, Any] = dict(
            status=RunStatus.OUTCOME_UNKNOWN,
            defer_pending=False,
            error_code=ErrorCode.OUTCOME_UNKNOWN.value,
        )
        if self.state.pending is not None and self.state.pending.operation == "cancel":
            changes["remote_cancel_status"] = "outcome_unknown"
        self._save(self.state.model_copy(update=changes), "a2a.outcome_unknown")
        self.queue.finish(self.lease, "outcome_unknown")

    def _observe(self, outcome: CallOutcome) -> None:
        assert self.state.pending is not None
        call = self.state.pending
        if outcome.error is not None and outcome.error.code == ErrorCode.OUTCOME_UNKNOWN:
            self._unknown()
            return
        observation = A2AObservation(
            call=call,
            trace=outcome.trace,
            error_code=outcome.error.code.value if outcome.error else None,
        )
        changes: dict[str, Any] = dict(
            pending=None,
            observations=(*self.state.observations, observation),
            defer_pending=False,
            waiting_for=None,
            error_code=None,
        )
        if outcome.error is not None:
            changes.update(
                status=RunStatus.CANCELLED if call.operation == "cancel" else RunStatus.FAILED,
                error_code=outcome.error.code.value,
            )
            if call.operation == "cancel":
                changes["remote_cancel_status"] = "rejected"
        else:
            result = self.service.confirmed_result(
                self.lease.repository_id,
                self.lease.run_id,
                outcome.trace.response_ref,
                self.program,
            )
            if (
                result.request_ref != outcome.trace.request_ref
                or result.operation != call.operation
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "A2A 观察与确切未决请求不符")
            remote = result.value()["status"]["state"]
            changes.update(
                last_response_ref=outcome.trace.response_ref,
                remote_state=remote,
                next_message=self.state.next_message + (call.operation == "send"),
            )
            if call.operation == "cancel":
                changes.update(
                    status=RunStatus.CANCELLED,
                    remote_cancel_status="confirmed"
                    if remote == "TASK_STATE_CANCELED"
                    else "not_confirmed",
                )
            elif remote == "TASK_STATE_COMPLETED":
                changes["status"] = RunStatus.COMPLETED
            elif remote in ("TASK_STATE_FAILED", "TASK_STATE_REJECTED"):
                changes.update(
                    status=RunStatus.FAILED, error_code=ErrorCode.VALIDATION_FAILED.value
                )
            elif remote == "TASK_STATE_CANCELED":
                changes["status"] = RunStatus.CANCELLED
            elif remote == "TASK_STATE_AUTH_REQUIRED":
                changes.update(status=RunStatus.PAUSED, waiting_for="auth")
            elif remote == "TASK_STATE_INPUT_REQUIRED" and changes["next_message"] >= len(
                self.program.messages
            ):
                changes.update(status=RunStatus.PAUSED, waiting_for="input")
            else:
                changes.update(status=RunStatus.RUNNING, defer_pending=True)
        action = self.queue.apply_control(self.lease)
        if action == "pause":
            changes.update(status=RunStatus.PAUSED, defer_pending=False)
        elif action == "budget_exhausted":
            changes.update(
                status=RunStatus.BUDGET_EXHAUSTED,
                defer_pending=False,
                error_code=ErrorCode.BUDGET_EXHAUSTED.value,
            )
        elif action == "cancel" and call.operation != "cancel":
            changes["cancel_requested"] = True
            if changes.get("remote_state", self.state.remote_state) in REMOTE_TERMINAL:
                changes.update(
                    status=RunStatus.CANCELLED, defer_pending=False, remote_cancel_status="not_sent"
                )
            else:
                changes.update(
                    status=RunStatus.RUNNING,
                    defer_pending=True,
                    remote_cancel_status="pending_approval",
                )
        self._save(self.state.model_copy(update=changes), "a2a.observed")
        self.fault("a2a.after_observation")
        if self.state.defer_pending:
            self._release()
        else:
            self.queue.finish(self.lease, self.state.status.value)

    def _release(self) -> None:
        delay = (
            0
            if self.state.cancel_requested or self.state.remote_state == "TASK_STATE_INPUT_REQUIRED"
            else self.program.poll_interval_ms
        )
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == self.lease.job_id)
                .values(
                    status="queued",
                    worker=None,
                    lease_until=None,
                    available_at=now + timedelta(milliseconds=delay),
                    last_error=None,
                )
            )
        self.fault("a2a.after_defer")

    def _save(self, state: A2ARunState, event: str, *, retire_approvals: bool = False) -> None:
        state = parse_contract(encode(state.model_dump(mode="json")), A2ARunState)
        artifacts = []
        if state.last_response_ref is not None:
            result = self.service.confirmed_result(
                self.lease.repository_id, self.lease.run_id, state.last_response_ref, self.program
            )
            artifacts.append(
                ArtifactInput(
                    "a2a/task.json", encode(result.model_dump(mode="json")), "application/json"
                )
            )
            for i, value in enumerate(result.value().get("artifacts", [])):
                artifacts.append(
                    ArtifactInput(f"a2a/artifact-{i + 1}.json", encode(value), "application/json")
                )
        if (
            len(artifacts) > self.program.limits.max_files
            or sum(len(a.content) for a in artifacts) > self.program.limits.max_artifact_bytes
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "A2A 本地产物超过固定预算")

        def projection(connection: Connection) -> None:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(status=state.status.value, failure_code=state.error_code)
            )
            if retire_approvals:
                approvals = TABLES["approvals"]
                connection.execute(
                    approvals.update()
                    .where(
                        approvals.c.repository_id == self.lease.repository_id,
                        approvals.c.run_id == self.lease.run_id,
                        approvals.c.status.in_(("pending", "approved")),
                        approvals.c.consumed_call_id.is_(None),
                    )
                    .values(status="expired")
                )
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            if (
                state.status in (RunStatus.PAUSED, RunStatus.CANCELLED, RunStatus.BUDGET_EXHAUSTED)
                or run["control_request"] == "resume"
                or (run["control_request"] == "cancel" and state.cancel_requested)
            ) and run["control_status"] == "requested":
                self.queue.mark_control_applied(connection, self.lease)

        commit = self.history.commit(
            self.lease.repository_id,
            self.lease.run_id,
            expected_head=self.head,
            expected_version=self.version,
            state=state.model_dump(mode="json"),
            environment=self.environment,
            events=(
                EventInput(
                    event,
                    self.state.pending_step if hasattr(self, "state") else "a2a",
                    dict(
                        program_ref=state.program_ref,
                        response_ref=state.last_response_ref,
                        error_code=state.error_code,
                        defer_pending=state.defer_pending,
                        remote_cancel_status=state.remote_cancel_status,
                    ),
                ),
            ),
            artifacts=tuple(artifacts),
            projection=projection,
        )
        self.head, self.version, self.state = commit.commit_id, commit.branch_version, state
        self.fault("a2a.after_checkpoint")

    def _reject(self, error: AgentGitError) -> None:
        if (
            hasattr(self, "state")
            and hasattr(self, "environment")
            and self.state.status not in STOPPED
        ):
            self._save(
                self.state.model_copy(
                    update=dict(
                        status=RunStatus.FAILED, defer_pending=False, error_code=error.code.value
                    )
                ),
                "a2a.rejected",
            )
            self.queue.finish(self.lease, "failed")
            return
        with self.history.engine.begin() as connection:
            self.queue.guard(connection, self.lease, self.lease.run_id)
            run = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
            event = EventInput("a2a.rejected", "a2a", {"error_code": error.code.value})
            obj = self.history.objects.put_json(
                ObjectType.EVENT, {"schema_version": 1, "data": event.payload}
            )
            self.history._event(
                connection,
                self.lease.repository_id,
                self.lease.run_id,
                run["last_event_seq"] + 1,
                event,
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
