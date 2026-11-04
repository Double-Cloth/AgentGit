"""Recorded 角色按自身来源重算、耐久落账和接管，不构造执行客户端。"""

from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.collaboration import CausalEventAddress, CollaborationRole, MessageKind
from agentgit.core.collaboration_execution import CollaborationRoleState
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import evolve
from agentgit.core.snapshots import EventManifest
from agentgit.services.collaboration_control import wake_stopped
from agentgit.services.collaboration_exchange import CollaborationExchange
from agentgit.services.collaboration_recorded_lane import RecordedRoleLane, verified_role_lane
from agentgit.services.history import TABLES, EventInput
from agentgit.services.jobs import JobService
from agentgit.storage.objects import StoredObject


class CollaborationRecordedRoleRunner:
    def __init__(self, queue: JobService, lease: Lease, fault: Callable[[str], None] | None = None):
        self.queue, self.lease = queue, lease
        self.fault = fault or (lambda _: None)
        self.exchange = CollaborationExchange(
            queue, lease, fault=lambda p: self.fault("collaboration.recorded." + p)
        )
        if self.exchange.payload.recording_ref is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 角色缺少固定来源")

    def _stop(self, status: RunStatus, code: str | None = None) -> dict[str, Any]:
        run = self.queue.run(self.lease.repository_id, self.lease.run_id)
        if run["status"] == status.value and run["checkpoint_commit_id"] is not None:
            if self.exchange.load().status != status:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 停止终点与 Run 不符")
            self.queue.finish(self.lease, status.value)
            return self.summary()
        with self.queue.history.engine.connect() as c:
            groups = TABLES["collaborations"]
            group = (
                c.execute(
                    sa.select(groups).where(
                        groups.c.repository_id == self.lease.repository_id,
                        groups.c.id == self.exchange.group_id,
                    )
                )
                .mappings()
                .one()
            )
        events = (
            ()
            if group["failure_run_id"] is None or group["failure_run_id"] == self.lease.run_id
            else (
                EventInput(
                    "collaboration.parent_stopped",
                    "collaboration",
                    dict(collaboration_id=str(self.exchange.group_id)),
                    caused_by_run_id=group["failure_run_id"],
                    caused_by_seq=group["failure_event_seq"],
                ),
            )
        )
        current = self.exchange.load()
        usage = evolve(current.usage, reserved_tokens=0)
        if current.pending is not None:
            usage = evolve(
                usage,
                steps=usage.steps - 1,
                model_calls=usage.model_calls - int(current.pending.category == "model"),
                tool_calls=usage.tool_calls - int(current.pending.category == "tool"),
            )
        state = evolve(
            current, status=status, phase="done", pending=None, usage=usage, error_code=code
        )

        def project(c: Any) -> None:
            groups = TABLES["collaborations"]
            selected = c.execute(
                sa.select(groups.c.status)
                .where(groups.c.id == self.exchange.group_id)
                .with_for_update()
            ).scalar_one()
            if selected not in (
                "completed",
                "cancelled",
                "failed",
                "budget_exhausted",
                "outcome_unknown",
            ):
                current_run = self.exchange.history._run(
                    c, self.lease.repository_id, self.lease.run_id
                )
                c.execute(
                    groups.update()
                    .where(groups.c.id == self.exchange.group_id)
                    .values(
                        status=status.value,
                        failure_run_id=self.lease.run_id,
                        failure_event_seq=current_run["last_event_seq"],
                    )
                )
            wake_stopped(c, self.lease.repository_id, self.exchange.group_id)

        self.exchange.save(
            state, "collaboration.stopped", apply_control=True, events=events, projection=project
        )
        self.queue.finish(self.lease, status.value)
        return self.summary()

    def group(self) -> Any:
        with self.queue.history.engine.connect() as c:
            table = TABLES["collaborations"]
            return (
                c.execute(
                    sa.select(table).where(
                        table.c.repository_id == self.lease.repository_id,
                        table.c.id == self.exchange.group_id,
                    )
                )
                .mappings()
                .one()
            )

    def ready(self, state: CollaborationRoleState) -> bool:
        if state.role == CollaborationRole.SUPERVISOR and not state.sent:
            return True
        deps = {
            "supervisor": ("writer",),
            "researcher": (),
            "verifier": ("researcher",),
            "writer": ("researcher", "verifier"),
        }[state.role.value]
        summary = self.exchange.base.summary(self.lease.repository_id, self.exchange.group_id)
        statuses = {
            r["role"]: self.queue.run(self.lease.repository_id, UUID(r["run_id"]))["status"]
            for r in summary["roles"]
        }
        kinds = {e.kind for _, e, _ in self.exchange.inbox()}
        needed = {
            "supervisor": {"result"},
            "researcher": {"dispatch"},
            "verifier": {"dispatch", "evidence"},
            "writer": {"dispatch", "evidence", "verification"},
        }[state.role.value]
        return all(statuses[d] == "completed" for d in deps) and needed <= kinds

    def parent_map(
        self, lane: RecordedRoleLane, source: CollaborationRoleState
    ) -> dict[CausalEventAddress, CausalEventAddress]:
        result = {}
        for head in source.publications:
            original = head.publisher_event
            with self.queue.history.engine.connect() as c:
                table = TABLES["collaboration_recorded_steps"]
                row = (
                    c.execute(
                        sa.select(table).where(
                            table.c.repository_id == self.lease.repository_id,
                            table.c.run_id == self.lease.run_id,
                            table.c.source_commit_id == original.commit_id,
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "共享版本没有实际新导出检查点")
            manifest = self.queue.history.objects.get_json(row["commit_id"], ObjectType.COMMIT)
            events = parse_contract(
                self.queue.history.objects.get(
                    manifest["event_manifest_ref"], ObjectType.EVENT_MANIFEST
                ),
                EventManifest,
            )
            (event,) = [e for e in events.events if e.type == "collaboration.export_ready"]
            result[original] = CausalEventAddress(
                agent_run_id=str(self.lease.run_id),
                commit_id=row["commit_id"],
                seq=event.seq,
                payload_ref=event.payload_ref,
            )
        return result

    def materialize(
        self,
        c: Any,
        lane: RecordedRoleLane,
        source: CollaborationRoleState,
        previous: CollaborationRoleState | None,
    ) -> None:
        old = len(previous.traces) if previous is not None else 0
        calls = TABLES["external_calls"]
        for record in lane.recordings[old : len(source.traces)]:
            origin = (
                c.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == self.lease.repository_id,
                        calls.c.run_id == UUID(lane.source.run_id),
                        calls.c.id == UUID(record.source_call_id),
                    )
                )
                .mappings()
                .one()
            )
            existing = c.execute(
                sa.select(calls.c.id).where(
                    calls.c.repository_id == self.lease.repository_id,
                    calls.c.run_id == self.lease.run_id,
                    calls.c.logical_step_id == origin["logical_step_id"],
                    calls.c.call_index == origin["call_index"],
                    calls.c.attempt == origin["attempt"],
                )
            ).first()
            if existing is not None:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未推进的来源检查点已有调用")
            identity = uuid4()
            for role, ref in (
                ("request", origin["request_digest"]),
                ("intent", origin["intent_ref"]),
                ("result", origin["result_ref"]),
                ("response", record.saved.response_ref),
            ):
                objects = TABLES["objects"]
                row = c.execute(sa.select(objects).where(objects.c.id == ref)).mappings().one()
                obj = StoredObject(
                    row["id"],
                    ObjectType(row["object_type"]),
                    row["media_type"],
                    row["byte_length"],
                    row["storage_key"],
                    row["codec"],
                )
                self.exchange.history._ref(
                    c, self.lease.repository_id, obj, "external_call", str(identity), role
                )
            c.execute(
                calls.insert().values(
                    id=identity,
                    repository_id=self.lease.repository_id,
                    run_id=self.lease.run_id,
                    origin_call_id=origin["id"],
                    **{
                        k: origin[k]
                        for k in (
                            "logical_step_id",
                            "call_index",
                            "attempt",
                            "provider",
                            "signature_digest",
                            "request_digest",
                            "effect",
                            "intent_ref",
                            "result_ref",
                            "status",
                        )
                    },
                )
            )

    def execute(self) -> dict[str, Any]:
        lane = verified_role_lane(self.queue, self.lease)
        job = self.queue.get(self.lease.repository_id, self.lease.job_id)
        self.exchange.environment = evolve(lane.environment, input_ref=job["payload_ref"])
        table = TABLES["collaboration_recorded_steps"]
        with self.queue.history.engine.connect() as c:
            saved = list(
                c.execute(
                    sa.select(table).where(
                        table.c.repository_id == self.lease.repository_id,
                        table.c.run_id == self.lease.run_id,
                    )
                ).mappings()
            )
        positions = {f.commit_id: i for i, f in enumerate(lane.source.frames)}
        saved.sort(key=lambda r: positions.get(r["source_commit_id"], -1))
        if [r["source_commit_id"] for r in saved] != [
            f.commit_id for f in lane.source.frames[: len(saved)]
        ] or any(
            str(r["source_run_id"]) != lane.source.run_id
            or r["source_index_ref"] != self.exchange.payload.recording_ref
            for r in saved
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 进度不是自身完整来源前缀")
        current = self.exchange.load()
        if saved:
            expected = parse_contract(
                encode(
                    self.queue.history.restore(
                        self.lease.repository_id, saved[-1]["commit_id"]
                    ).data
                ),
                CollaborationRoleState,
            )
            if evolve(current, status=expected.status, error_code=expected.error_code) != expected:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 接管状态与已提交进度不符")
        for index in range(len(saved), len(lane.source.frames)):
            control = self.queue.apply_control(self.lease)
            group = self.group()
            if control == "cancel" or group["status"] in (
                "cancelled",
                "failed",
                "budget_exhausted",
                "outcome_unknown",
            ):
                return self._stop(RunStatus.CANCELLED)
            if control == "pause" or group["status"] == "paused":
                state = evolve(self.exchange.load(), status=RunStatus.PAUSED)
                self.exchange.save(state, "collaboration.stopped", apply_control=True)
                self.queue.finish(self.lease, "paused")
                return self.summary()
            if control == "budget_exhausted":
                return self._stop(RunStatus.BUDGET_EXHAUSTED, ErrorCode.BUDGET_EXHAUSTED.value)
            if not self.ready(self.exchange.load()):
                self.queue.finish(self.lease, "paused")
                return self.summary()
            if control == "resume":
                self.exchange.save(
                    evolve(self.exchange.load(), status=RunStatus.RUNNING, error_code=None),
                    "collaboration.resumed",
                    apply_control=True,
                )
                # 恢复请求应用后仍检查原截止时间，不能多消费一个来源步骤。
                if self.queue.apply_control(self.lease) == "budget_exhausted":
                    return self._stop(RunStatus.BUDGET_EXHAUSTED, ErrorCode.BUDGET_EXHAUSTED.value)
            frame, source, event_type = (
                lane.source.frames[index],
                lane.states[index],
                lane.event_types[index],
            )
            mapped = lane.mapping.state(source, self.parent_map(lane, source))
            previous = lane.states[index - 1] if index else None
            detail = dict(
                recording=dict(
                    source_index_ref=self.exchange.payload.recording_ref,
                    source_run_id=lane.source.run_id,
                    source_commit_id=frame.commit_id,
                    source_state_ref=frame.state_ref,
                    event_manifest_ref=frame.event_manifest_ref,
                )
            )

            def project(
                c: Any, frame: Any = frame, source: Any = source, previous: Any = previous
            ) -> None:
                self.queue.guard(c, self.lease, self.lease.run_id)
                group_row = (
                    c.execute(
                        sa.select(TABLES["collaborations"])
                        .where(TABLES["collaborations"].c.id == self.exchange.group_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                run = self.exchange.history._run(c, self.lease.repository_id, self.lease.run_id)
                if group_row["status"] not in ("running", "completed") or (
                    run["control_status"] == "requested"
                    and run["control_request"] in ("pause", "cancel")
                ):
                    raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "Recorded 推进前收到控制请求")
                self.materialize(c, lane, source, previous)
                c.execute(
                    table.insert().values(
                        repository_id=self.lease.repository_id,
                        run_id=self.lease.run_id,
                        source_run_id=UUID(lane.source.run_id),
                        source_commit_id=frame.commit_id,
                        commit_id=run["checkpoint_commit_id"],
                        source_index_ref=self.exchange.payload.recording_ref,
                    )
                )
                if (
                    source.status == RunStatus.COMPLETED
                    and source.role == CollaborationRole.SUPERVISOR
                ):
                    c.execute(
                        TABLES["collaborations"]
                        .update()
                        .where(TABLES["collaborations"].c.id == self.exchange.group_id)
                        .values(status="completed")
                    )

            if event_type == "collaboration.inbox_observed":
                state = self.exchange.load()
                self.exchange.receive(
                    state,
                    order=tuple(m for m in mapped.received if m not in state.received),
                    expected_state=mapped,
                    after_project=project,
                    details=detail,
                )
            elif event_type == "collaboration.role_transition":
                state = self.exchange.load()
                routes = {
                    "supervisor": (
                        ("researcher", "dispatch", "message:collect"),
                        ("verifier", "dispatch", "message:verify"),
                        ("writer", "dispatch", "message:write"),
                    ),
                    "researcher": (
                        ("verifier", "evidence", "message"),
                        ("writer", "evidence", "message"),
                    ),
                    "verifier": (("writer", "verification", "message"),),
                    "writer": (("supervisor", "result", "message"),),
                }[source.role.value]
                self.exchange.batch(
                    state,
                    sends=tuple(
                        (CollaborationRole(r), cast(MessageKind, k), state.exports[p])
                        for r, k, p in routes
                    ),
                    publications=tuple(
                        (h.path, h.version - 1, h.object_ref)
                        for h in mapped.publications[len(state.publications) :]
                    ),
                    status=mapped.status,
                    phase=mapped.phase,
                    expected_state=mapped,
                    after_project=project,
                    details=detail,
                )
            else:
                added = lane.recordings[
                    len(previous.traces) if previous else 0 : len(source.traces)
                ]
                # 录制调用的实际来源在同事务账本固定；事件明确标为复用，不伪造新的请求。
                events = tuple(
                    EventInput(
                        "call.reused",
                        r.intent.occurrence.logical_step_id,
                        dict(
                            occurrence=r.intent.occurrence.model_dump(mode="json"),
                            origin_run_id=lane.source.run_id,
                            origin_call_id=r.source_call_id,
                            response_ref=r.saved.response_ref,
                        ),
                    )
                    for r in added
                )
                self.exchange.save(
                    mapped, event_type, details=detail, events=events, projection=project
                )
            self.fault("collaboration.recorded_frame_saved")
        self.queue.finish(self.lease, "completed")
        return self.summary()

    def run(self) -> dict[str, Any]:
        while True:
            run = self.queue.run(self.lease.repository_id, self.lease.run_id)
            if run["status"] in ("completed", "cancelled", "budget_exhausted"):
                self.queue.finish(self.lease, run["status"])
                return self.summary()
            control = self.queue.apply_control(self.lease)
            if control == "cancel" or self.group()["status"] in (
                "cancelled",
                "failed",
                "budget_exhausted",
                "outcome_unknown",
            ):
                return self._stop(RunStatus.CANCELLED)
            if control == "budget_exhausted":
                return self._stop(RunStatus.BUDGET_EXHAUSTED, ErrorCode.BUDGET_EXHAUSTED.value)
            if self.group()["status"] == "prepared":
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Recorded 必须由本人明确开始")
            try:
                return self.execute()
            except AgentGitError as error:
                if error.code != ErrorCode.CONTROL_REQUESTED:
                    raise
                if str(error) == "budget_exhausted":
                    return self._stop(RunStatus.BUDGET_EXHAUSTED, ErrorCode.BUDGET_EXHAUSTED.value)

    def summary(self) -> dict[str, Any]:
        return dict(
            job_id=str(self.lease.job_id),
            run_id=str(self.lease.run_id),
            status=self.queue.get(self.lease.repository_id, self.lease.job_id)["status"],
        )
