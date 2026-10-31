"""A2A Recorded 独立耐久任务；只重算历史，每次领取推进一个检查点。"""

import platform
from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.core.a2a_runs import A2ARecordedRunState
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import CommitManifest, EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.snapshots import EventManifest
from agentgit.replay.a2a import A2ARecomputedFrame, A2ARecordedEngine, require
from agentgit.services.a2a_recordings import A2ARecordingReader
from agentgit.services.a2a_runs import A2ARunService
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService
from agentgit.services.jobs import JobService

TERMINAL = {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.BUDGET_EXHAUSTED}


class A2ARecordedRunner:
    def __init__(self, queue: JobService, lease: Lease, *, fault: Callable[[str], None]):
        self.queue, self.lease, self.fault = queue, lease, fault

        def guard(c: Connection, run: UUID) -> None:
            queue.guard(c, lease, run)

        self.history = HistoryService(
            queue.history.engine,
            queue.history.objects,
            lease_guard=guard,
        )
        self.service = A2ARunService(queue)
        self.payload = self.service.payload(lease.repository_id, lease.job_id)
        self.artifacts: tuple[ArtifactInput, ...] = ()

    def run(self) -> dict[str, Any]:
        try:
            self.execute()
        except AgentGitError as error:
            if error.code == ErrorCode.VERSION_CONFLICT:
                return dict(
                    job_id=str(self.lease.job_id),
                    run_id=str(self.lease.run_id),
                    status="lease_lost",
                    error_code=error.code.value,
                )
            if error.code == ErrorCode.CONTROL_REQUESTED:
                self.release()
                job = self.queue.get(self.lease.repository_id, self.lease.job_id)
                return dict(
                    job_id=str(self.lease.job_id),
                    run_id=str(self.lease.run_id),
                    status=job["status"],
                    error_code=job["last_error"],
                )
            if not hasattr(self, "state"):
                raise
            if hasattr(self, "frames"):
                self.artifacts = self.frame_artifacts(self.frames[self.state.cursor])
            self.save(
                self.state.model_copy(
                    update=dict(status=RunStatus.FAILED, error_code=error.code.value)
                ),
                "a2a.recorded_rejected",
            )
            self.queue.finish(self.lease, "failed")
        job = self.queue.get(self.lease.repository_id, self.lease.job_id)
        return dict(
            job_id=str(self.lease.job_id),
            run_id=str(self.lease.run_id),
            status=job["status"],
            error_code=job["last_error"],
        )

    def execute(self) -> None:
        payload, lease = self.payload, self.lease
        if payload.replay is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A Recorded Job缺少明确来源")
        if (
            payload.code_revision != source_revision()
            or payload.dependency_lock_digest != dependency_lock_digest()
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "A2A Recorded 源码或锁与Worker不符")
        program = self.service.program(lease.repository_id, payload)
        require(program.limits.max_wall_ms == payload.max_wall_ms, "A2A Recorded 原墙钟预算不符")
        self.environment = EnvironmentManifest(
            runtime_version=__version__,
            python_version=platform.python_version(),
            code_revision=payload.code_revision,
            dependency_lock_digest=payload.dependency_lock_digest,
            model_signature="no-model/fixed-a2a-recorded/v1",
            prompt_digest=object_digest(ObjectType.BLOB, b"fixed-a2a-recorded/v1"),
            input_ref=payload.program_ref,
            evaluation_time="2026-11-01T00:00:00Z",
            random_seed=0,
        )
        with self.history.engine.begin() as c:
            job = self.queue.guard(c, lease, lease.run_id)
            run = self.history._run(c, lease.repository_id, lease.run_id)
            require(
                run["mode"] == "recorded"
                and str(run["origin_run_id"]) == payload.replay.source_run_id
                and str(job["origin_run_id"]) == payload.replay.source_run_id
                and run["base_commit_id"] == payload.replay.from_commit,
                "A2A Recorded Run／Job与固定来源不符",
            )
        self.head = run["checkpoint_commit_id"]
        self.version = self.history.branch(lease.repository_id, run["branch_name"])["version"]
        restored = self.service.recorded_state(
            lease.repository_id, lease.run_id, actor=payload.actor
        )
        if restored is not None:
            self.state = restored
            snapshot = self.history.restore(lease.repository_id, self.head)
            self.artifacts = tuple(
                ArtifactInput(
                    a.path, self.history.objects.get(a.object_ref, ObjectType.BLOB), a.media_type
                )
                for a in snapshot.artifacts
            )
        source = A2ARecordingReader(self.history).inspect(
            lease.repository_id, payload.replay, actor=payload.actor
        )
        require(
            source.program == program and source.program_ref == payload.program_ref,
            "A2A Recorded 程序与来源不符",
        )
        self.source = source
        self.frames = A2ARecordedEngine(source).replay()
        self.validate_progress()
        self.fault("a2a.recorded.after_source")
        if restored is None:
            frame = self.frames[0]
            self.artifacts = self.frame_artifacts(frame)
            self.state = A2ARecordedRunState(
                program_ref=payload.program_ref,
                replay=payload.replay,
                cursor=0,
                logical_state=frame.state,
            )
            action = self.action()
            self.save(self.control_state(action), "a2a.recorded_step", cursor=0)
            self.finish_or_release()
            return
        if self.state.status in TERMINAL:
            self.queue.finish(lease, self.state.status.value)
            return
        action = self.action()
        if action in {"pause", "cancel", "budget_exhausted"}:
            self.save(self.control_state(action), "a2a.recorded_control")
            self.finish_or_release()
            return
        if self.state.status == RunStatus.PAUSED:
            if action != "resume":
                self.queue.finish(lease, "paused")
                return
            self.save(
                self.state.model_copy(update=dict(status=RunStatus.RUNNING, error_code=None)),
                "a2a.recorded_control",
            )
            self.release()
            return
        cursor = self.state.cursor + 1
        require(cursor < len(self.frames), "A2A Recorded 完成检查点缺少本地终态")
        frame = self.frames[cursor]
        self.artifacts = self.frame_artifacts(frame)
        state = self.state.model_copy(
            update=dict(
                cursor=cursor,
                logical_state=frame.state,
                status=RunStatus.COMPLETED if cursor == len(self.frames) - 1 else RunStatus.RUNNING,
            )
        )
        self.save(state, "a2a.recorded_step", cursor=cursor)
        self.finish_or_release()

    @staticmethod
    def frame_artifacts(frame: A2ARecomputedFrame) -> tuple[ArtifactInput, ...]:
        return tuple(ArtifactInput(name, raw, "application/json") for name, raw in frame.artifacts)

    def action(self) -> str | None:
        with self.history.engine.begin() as c:
            job = self.queue.guard(c, self.lease, self.lease.run_id)
            run = self.history._run(c, self.lease.repository_id, self.lease.run_id)
            self.control_identity = (run["control_request"], run["control_status"])
            now = c.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            self.expired = job["deadline_at"] <= now
            if self.expired:
                return "budget_exhausted"
            return str(run["control_request"]) if run["control_status"] == "requested" else None

    def control_state(self, action: str | None) -> A2ARecordedRunState:
        status = {
            "pause": RunStatus.PAUSED,
            "cancel": RunStatus.CANCELLED,
            "budget_exhausted": RunStatus.BUDGET_EXHAUSTED,
        }.get(action or "", self.state.status)
        return self.state.model_copy(
            update=dict(
                status=status,
                error_code=ErrorCode.BUDGET_EXHAUSTED.value
                if action == "budget_exhausted"
                else None,
            )
        )

    def validate_progress(self) -> None:
        links, snapshots, calls = (
            TABLES["run_commits"],
            TABLES["snapshots"],
            TABLES["external_calls"],
        )
        with self.history.engine.connect() as c:
            paths = list(
                c.execute(
                    sa.select(links)
                    .where(
                        links.c.repository_id == self.lease.repository_id,
                        links.c.run_id == self.lease.run_id,
                    )
                    .order_by(links.c.run_order)
                    .limit(386)
                ).mappings()
            )
            count = c.execute(
                sa.select(sa.func.count())
                .select_from(snapshots)
                .where(
                    snapshots.c.run_id == self.lease.run_id,
                    snapshots.c.repository_id == self.lease.repository_id,
                )
            ).scalar_one()
            own_calls = c.execute(
                sa.select(calls.c.id)
                .where(
                    calls.c.repository_id == self.lease.repository_id,
                    calls.c.run_id == self.lease.run_id,
                )
                .limit(1)
            ).first()
        require(
            len(paths) < 386 and count == len(paths) and own_calls is None,
            "A2A Recorded 进度索引超限、缺失或混入实际调用",
        )
        cursor, previous = -1, self.payload.replay.from_commit if self.payload.replay else None
        for order, row in enumerate(paths, 1):
            require(row["run_order"] == order, "A2A Recorded 检查点顺序缺失")
            snapshot = self.history.restore(self.lease.repository_id, row["commit_id"])
            state = parse_contract(encode(snapshot.data), A2ARecordedRunState)
            require(
                state.program_ref == self.payload.program_ref
                and state.replay == self.payload.replay
                and state.cursor < len(self.frames)
                and state.cursor in {cursor, cursor + 1},
                "A2A Recorded 进度不属于自身连续来源前缀",
            )
            frame = self.frames[state.cursor]
            require(
                state.logical_state == frame.state
                and snapshot.artifacts == frame.snapshot.artifacts,
                "A2A Recorded 耐久逻辑状态或产物与重算不符",
            )
            manifest = parse_contract(
                self.history.objects.get(row["commit_id"], ObjectType.COMMIT), CommitManifest
            )
            require(manifest.parents == (previous,), "A2A Recorded 本地父链不符")
            events = parse_contract(
                self.history.objects.get(manifest.event_manifest_ref, ObjectType.EVENT_MANIFEST),
                EventManifest,
            )
            steps = [e for e in events.events if e.type == "a2a.recorded_step"]
            consumed = [e for e in events.events if e.type == "call.recorded"]
            advance = state.cursor == cursor + 1
            require(
                len(steps) == int(advance)
                and len(consumed) == int(advance and state.cursor > 0 and state.cursor % 2 == 0),
                "A2A Recorded 来源消费与检查点推进不符",
            )
            for event in (*steps, *consumed):
                expected = (
                    self.step_detail(state.cursor)
                    if event.type == "a2a.recorded_step"
                    else self.call_detail(state.cursor)
                )
                actual = self.history.objects.get_json(event.payload_ref, ObjectType.EVENT)
                require(actual["data"] == expected, "A2A Recorded 来源消费事件不符")
            cursor, previous = state.cursor, row["commit_id"]
        require(
            previous == self.head
            and (
                (cursor == -1 and not hasattr(self, "state"))
                or (hasattr(self, "state") and cursor == self.state.cursor)
            ),
            "A2A Recorded HEAD与耐久进度不符",
        )

    def step_detail(self, cursor: int) -> dict[str, Any]:
        return dict(
            source_run_id=self.source.spec.source_run_id,
            source_commit=self.frames[cursor].source_commit,
            source_state_ref=self.source.frames[cursor].state_ref,
            cursor=cursor,
            actual_attempts=0,
        )

    def call_detail(self, cursor: int) -> dict[str, Any]:
        record = self.source.recordings[cursor // 2 - 1]
        return dict(
            source_run_id=self.source.spec.source_run_id,
            source_call_id=record.source_call_id,
            occurrence=record.intent.occurrence.model_dump(mode="json"),
            source_checkpoint=record.intent.checkpoint,
            response_ref=record.saved.response_ref,
            actual_attempts=0,
        )

    def save(self, state: A2ARecordedRunState, event: str, *, cursor: int | None = None) -> None:
        state = parse_contract(encode(state.model_dump(mode="json")), A2ARecordedRunState)
        detail = (
            self.step_detail(cursor)
            if cursor is not None
            else dict(
                cursor=state.cursor,
                status=state.status.value,
                error_code=state.error_code,
                control_scope="local_recorded",
            )
        )
        events = []
        if cursor is not None and cursor > 0 and cursor % 2 == 0:
            record = self.source.recordings[cursor // 2 - 1]
            events.append(
                EventInput(
                    "call.recorded",
                    record.intent.occurrence.logical_step_id,
                    self.call_detail(cursor),
                )
            )
        events.append(EventInput(event, "a2a-recorded", detail))

        def projection(c: Connection) -> None:
            job = self.queue.guard(c, self.lease, self.lease.run_id)
            run = self.history._run(c, self.lease.repository_id, self.lease.run_id)
            now = c.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            if hasattr(self, "control_identity") and (
                (run["control_request"], run["control_status"]) != self.control_identity
                or (job["deadline_at"] <= now and not self.expired)
            ):
                raise AgentGitError(
                    ErrorCode.CONTROL_REQUESTED, "A2A Recorded 提交前控制或原deadline已变化"
                )
            c.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(status=state.status.value, failure_code=state.error_code)
            )
            self.queue.mark_control_applied(c, self.lease)
            self.fault("a2a.recorded.in_projection")

        self.fault("a2a.recorded.before_checkpoint")
        commit = self.history.commit(
            self.lease.repository_id,
            self.lease.run_id,
            expected_head=self.head,
            expected_version=self.version,
            state=state.model_dump(mode="json"),
            environment=self.environment,
            events=tuple(events),
            artifacts=self.artifacts,
            projection=projection,
        )
        self.head, self.version, self.state = commit.commit_id, commit.branch_version, state
        self.fault("a2a.recorded.after_checkpoint")

    def finish_or_release(self) -> None:
        if self.state.status in TERMINAL or self.state.status == RunStatus.PAUSED:
            self.queue.finish(self.lease, self.state.status.value)
        else:
            self.release()

    def release(self) -> None:
        with self.history.engine.begin() as c:
            self.queue.guard(c, self.lease, self.lease.run_id)
            c.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == self.lease.job_id)
                .values(
                    status="queued",
                    worker=None,
                    lease_until=None,
                    available_at=sa.func.clock_timestamp(),
                    checkpoint_commit_id=self.head,
                    last_error=None,
                )
            )
        self.fault("a2a.recorded.after_release")
