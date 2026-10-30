"""租约角色检查点及原子消息/共享投影；不将私有历史开放给其他角色。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus, utc_time
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.collaboration import (
    CausalEventAddress,
    CollaborationMessage,
    CollaborationProgram,
    CollaborationRole,
    MessageKind,
    SharedArtifactHead,
    SharedPath,
    advance_shared_artifact,
    authorize_message,
)
from agentgit.core.collaboration_execution import (
    CollaborationRoleState,
    DispatchExport,
    EvidenceExport,
    ResultExport,
    VerificationExport,
)
from agentgit.core.collaboration_runs import CollaborationRolePayload
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenCorpus
from agentgit.core.jobs import Lease
from agentgit.core.models import EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import AgentState, evolve
from agentgit.core.snapshots import EventManifest
from agentgit.runtime.validation import ResearchReport, validate_report
from agentgit.services.collaboration import CollaborationService
from agentgit.services.history import (
    TABLES,
    ArtifactInput,
    CommitResult,
    EventInput,
    HistoryService,
)
from agentgit.services.jobs import JobService

TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted", "outcome_unknown"}


def wake_collaboration(
    queue: JobService, connection: Connection, repository: UUID, group_id: UUID
) -> None:
    """只锁可领取的暂停 Job，不反向锁其他活动 Run。"""
    groups, tasks, runs, jobs, msgs = (
        TABLES[n]
        for n in ("collaborations", "collaboration_tasks", "runs", "jobs", "collaboration_messages")
    )
    group = (
        connection.execute(
            sa.select(groups)
            .where(groups.c.repository_id == repository, groups.c.id == group_id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    if group["status"] != "running":
        return
    members = (
        connection.execute(
            sa.select(
                tasks.c.role,
                tasks.c.run_id,
                runs.c.status,
                runs.c.control_request,
                runs.c.control_status,
                runs.c.checkpoint_commit_id,
            )
            .join(
                runs,
                sa.and_(runs.c.repository_id == tasks.c.repository_id, runs.c.id == tasks.c.run_id),
            )
            .where(tasks.c.repository_id == repository, tasks.c.collaboration_id == group_id)
        )
        .mappings()
        .all()
    )
    by_role = {r["role"]: r for r in members}
    for role, run in by_role.items():
        if run["status"] in TERMINAL or (
            run["control_request"] == "pause" and run["control_status"] == "applied"
        ):
            continue
        dependencies = {
            "supervisor": ("writer",),
            "researcher": (),
            "verifier": ("researcher",),
            "writer": ("researcher", "verifier"),
        }[role]
        if role == "supervisor" and run["checkpoint_commit_id"] is None:
            dependencies = ()
        if any(by_role[dep]["status"] != "completed" for dep in dependencies):
            continue
        expected = {
            "supervisor": ({"writer"} if dependencies else set()),
            "researcher": {"supervisor"},
            "verifier": {"supervisor", "researcher"},
            "writer": {"supervisor", "researcher", "verifier"},
        }[role]
        senders = set(
            connection.execute(
                sa.select(msgs.c.sender_run_id).where(
                    msgs.c.repository_id == repository,
                    msgs.c.collaboration_id == group_id,
                    msgs.c.receiver_run_id == run["run_id"],
                )
            ).scalars()
        )
        if not {by_role[r]["run_id"] for r in expected} <= senders:
            continue
        candidate = connection.execute(
            sa.select(jobs.c.id)
            .where(
                jobs.c.repository_id == repository,
                jobs.c.run_id == run["run_id"],
                jobs.c.status == "paused",
            )
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if candidate is not None:
            connection.execute(
                jobs.update()
                .where(jobs.c.id == candidate)
                .values(status="queued", available_at=sa.func.clock_timestamp())
            )


class CollaborationExchange:
    def __init__(
        self,
        queue: JobService,
        lease: Lease,
        environment: EnvironmentManifest | None = None,
        *,
        fault: Callable[[str], None] | None = None,
    ):
        self.queue, self.lease = queue, lease
        self.base = CollaborationService(queue)
        self.scope = self.base.role_input(lease)

        def guard(c: Connection, r: UUID) -> None:
            queue.guard(c, lease, r)

        self.history = HistoryService(
            queue.history.engine,
            queue.history.objects,
            lease_guard=guard,
            fault=fault,
        )
        self.payload = parse_contract(
            self.history.objects.get(
                queue.get(lease.repository_id, lease.job_id)["payload_ref"], ObjectType.BLOB
            ),
            CollaborationRolePayload,
        )
        self.group_id = UUID(self.scope.bindings.collaboration_id)
        self.task = self.scope.bindings.agent(str(lease.run_id))
        with self.history.engine.connect() as connection:
            program_raw = self.base._owned(
                connection,
                lease.repository_id,
                self.payload.program_ref,
                "collaboration",
                str(self.group_id),
                "program",
            )
            corpus_raw = self.base._owned(
                connection,
                lease.repository_id,
                self.scope.frozen_input.corpus_ref,
                "collaboration",
                str(self.group_id),
                "corpus",
            )
        self.program = parse_contract(program_raw, CollaborationProgram)
        frozen = parse_contract(corpus_raw, FrozenCorpus)
        self.corpus = Corpus(frozen.sources, evaluation_time=frozen.evaluation_time)
        self.environment = environment or self.scope.environment
        self.files: dict[str, bytes] = {}
        self.media: dict[str, str] = {}

    def load(self) -> CollaborationRoleState:
        run = self.queue.run(self.lease.repository_id, self.lease.run_id)
        if run["checkpoint_commit_id"] is None:
            return CollaborationRoleState(
                collaboration_id=str(self.group_id),
                role=self.payload.role,
                program_ref=self.payload.program_ref,
                private_input_ref=self.payload.private_input_ref,
            )
        snapshot = self.history.restore(self.lease.repository_id, run["checkpoint_commit_id"])
        state = parse_contract(encode(snapshot.data), CollaborationRoleState)
        if (state.collaboration_id, state.role, state.program_ref, state.private_input_ref) != (
            str(self.group_id),
            self.payload.role,
            self.payload.program_ref,
            self.payload.private_input_ref,
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "角色检查点与冻结输入不符")
        self.files = {
            a.path: self.history.objects.get(a.object_ref, ObjectType.BLOB)
            for a in snapshot.artifacts
        }
        self.media = {a.path: a.media_type for a in snapshot.artifacts}
        return state

    def save(
        self,
        state: CollaborationRoleState,
        event_type: str,
        *,
        details: dict[str, Any] | None = None,
        events: tuple[EventInput, ...] = (),
        projection: Callable[[Connection], None] | None = None,
        apply_control: bool = False,
    ) -> CommitResult:
        state = parse_contract(encode(state.model_dump(mode="json")), CollaborationRoleState)
        limits = self.scope.policy.limits
        u = state.usage
        if (
            u.steps > limits.max_steps
            or u.model_calls > limits.max_model_calls
            or u.tool_calls > limits.max_tool_calls
            or u.tokens + u.reserved_tokens > limits.max_tokens
        ):
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "角色独立预算已耗尽")
        if len(encode(state.model_dump(mode="json"))) > limits.max_context_bytes:
            raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "角色检查点超过独立上下文上限")
        files = {**self.files}
        media = {**self.media}
        for path, ref in state.exports.items():
            if path.startswith("message"):
                continue
            files[path] = self.history.objects.get(ref, ObjectType.BLOB)
            media[path] = "text/markdown" if path.endswith(".md") else "application/json"
        if (
            len(files) > limits.max_files
            or sum(map(len, files.values())) > limits.max_artifact_bytes
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "角色产物超过独立文件/字节预算")
        run = self.queue.run(self.lease.repository_id, self.lease.run_id)
        branch = self.history.branch(self.lease.repository_id, run["branch_name"])
        data = {
            "collaboration_id": str(self.group_id),
            "role": state.role.value,
            "program_ref": state.program_ref,
            **(details or {}),
        }

        def project(connection: Connection) -> None:
            job = self.queue.guard(connection, self.lease, self.lease.run_id)
            if event_type not in {
                "collaboration.call_observed",
                "collaboration.artifact_observed",
                "collaboration.stopped",
            }:
                current = self.history._run(connection, self.lease.repository_id, self.lease.run_id)
                if current["control_status"] == "requested" and current["control_request"] in (
                    "pause",
                    "cancel",
                ):
                    raise AgentGitError(ErrorCode.CONTROL_REQUESTED, current["control_request"])
                now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
                if job["deadline_at"] is not None and job["deadline_at"] <= now:
                    raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "budget_exhausted")
            for path, ref in state.exports.items():
                obj = self.history.objects.put(
                    ObjectType.BLOB, self.history.objects.get(ref, ObjectType.BLOB)
                )
                self.history._ref(
                    connection,
                    self.lease.repository_id,
                    obj,
                    "collaboration_export",
                    str(self.lease.run_id),
                    path,
                )
            if state.pending is not None:
                obj = self.history.objects.put(
                    ObjectType.BLOB,
                    self.history.objects.get(state.pending.request_ref, ObjectType.BLOB),
                )
                self.history._ref(
                    connection,
                    self.lease.repository_id,
                    obj,
                    "collaboration_task",
                    self.task.task_id,
                    "call_input",
                )
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.lease.run_id)
                .values(
                    status=state.status.value,
                    state_version=TABLES["runs"].c.state_version + 1,
                    failure_code=state.error_code,
                )
            )
            if apply_control:
                self.queue.mark_control_applied(connection, self.lease)
            if projection is not None:
                projection(connection)

        result = self.history.commit(
            self.lease.repository_id,
            self.lease.run_id,
            expected_head=run["checkpoint_commit_id"],
            expected_version=branch["version"],
            state=state.model_dump(mode="json"),
            environment=self.environment,
            events=(*events, EventInput(event_type, "collaboration", data)),
            artifacts=tuple(ArtifactInput(p, b, media[p]) for p, b in files.items()),
            projection=project,
        )
        self.files, self.media = files, media
        return result

    def _parent(self, address: CausalEventAddress, payload_ref: str) -> CollaborationRoleState:
        snapshot = self.history.restore(self.lease.repository_id, address.commit_id)
        state = parse_contract(encode(snapshot.data), CollaborationRoleState)
        if (
            state.collaboration_id != str(self.group_id)
            or state.program_ref != self.payload.program_ref
            or state.phase != "export_ready"
            or payload_ref not in state.exports.values()
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "消息/共享值必须来自本协作的明确导出检查点")
        self.scope.bindings.agent(address.agent_run_id)
        with self.history.engine.connect() as c:
            snapshots, events, commits = (TABLES[n] for n in ("snapshots", "events", "commits"))
            own = c.execute(
                sa.select(snapshots.c.commit_id).where(
                    snapshots.c.repository_id == self.lease.repository_id,
                    snapshots.c.run_id == UUID(address.agent_run_id),
                    snapshots.c.commit_id == address.commit_id,
                )
            ).first()
            event = (
                c.execute(
                    sa.select(events).where(
                        events.c.repository_id == self.lease.repository_id,
                        events.c.run_id == UUID(address.agent_run_id),
                        events.c.seq == address.seq,
                    )
                )
                .mappings()
                .first()
            )
            manifest_ref = c.execute(
                sa.select(commits.c.event_manifest_ref).where(
                    commits.c.repository_id == self.lease.repository_id,
                    commits.c.id == address.commit_id,
                )
            ).scalar_one_or_none()
        if (
            own is None
            or event is None
            or event["payload_ref"] != address.payload_ref
            or event["type"] != "collaboration.export_ready"
            or manifest_ref is None
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "导出父事件不属于声明的 Run/检查点")
        manifest = parse_contract(
            self.history.objects.get(manifest_ref, ObjectType.EVENT_MANIFEST), EventManifest
        )
        if not any(
            e.seq == address.seq and e.payload_ref == address.payload_ref for e in manifest.events
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "导出父事件不在声明的版本范围")
        if state.role != self.scope.bindings.agent(address.agent_run_id).role:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "导出状态角色与实际 Run 不符")
        return state

    def address(self) -> CausalEventAddress:
        with self.history.engine.connect() as c:
            events, snapshots = TABLES["events"], TABLES["snapshots"]
            row = (
                c.execute(
                    sa.select(events.c.seq, events.c.payload_ref, snapshots.c.commit_id)
                    .join(
                        snapshots,
                        sa.and_(
                            snapshots.c.repository_id == events.c.repository_id,
                            snapshots.c.run_id == events.c.run_id,
                            snapshots.c.event_seq == events.c.seq,
                        ),
                    )
                    .where(
                        events.c.repository_id == self.lease.repository_id,
                        events.c.run_id == self.lease.run_id,
                        events.c.type == "collaboration.export_ready",
                    )
                    .order_by(events.c.seq.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
        return CausalEventAddress(
            agent_run_id=str(self.lease.run_id),
            commit_id=row["commit_id"],
            seq=row["seq"],
            payload_ref=row["payload_ref"],
        )

    def evidence(self, raw: bytes) -> EvidenceExport:
        value = parse_contract(raw, EvidenceExport)
        if tuple(e.source.id for e in value.evidence) != self.corpus.search(
            self.scope.goal.frameworks
        ) or any(e.source != self.corpus.read(e.source.id) for e in value.evidence):
            raise AgentGitError(ErrorCode.VALIDATION_FAILED, "导出证据必须等于冻结语料的完整来源")
        return value

    def validate_payload(
        self, kind: str, ref: str, receiver: CollaborationRole | None = None
    ) -> Any:
        raw = self.history.objects.get(ref, ObjectType.BLOB)
        if len(raw) > self.scope.policy.limits.max_context_bytes:
            raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "导出超过当前角色上下文限制")
        if kind == "dispatch":
            value = parse_contract(raw, DispatchExport)
            if (
                value.program_ref != self.payload.program_ref
                or receiver is None
                or value.task
                != {"researcher": "collect", "verifier": "verify", "writer": "write"}.get(
                    receiver.value
                )
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "派发任务与固定接收角色不符")
            return value
        if kind == "evidence":
            return self.evidence(raw)
        if kind == "verification":
            verified = parse_contract(raw, VerificationExport)
            evidence = self.evidence(
                self.history.objects.get(verified.evidence_ref, ObjectType.BLOB)
            )
            if (
                verified.source_ids != tuple(e.source.id for e in evidence.evidence)
                or verified.evaluation_time != self.corpus.evaluation_time
                or any(
                    not (
                        utc_time(e.source.valid_from)
                        <= utc_time(verified.evaluation_time)
                        < utc_time(e.source.valid_until)
                    )
                    or utc_time(e.source.collected_at) > utc_time(verified.evaluation_time)
                    for e in evidence.evidence
                )
            ):
                raise AgentGitError(ErrorCode.VALIDATION_FAILED, "验证导出缺少完整有效来源")
            return verified
        if kind == "result":
            result = parse_contract(raw, ResultExport)
            verified = self.validate_payload("verification", result.verification_ref)
            if result.evidence_ref != verified.evidence_ref:
                raise AgentGitError(ErrorCode.VALIDATION_FAILED, "报告来源与验证范围不符")
            evidence = self.evidence(self.history.objects.get(result.evidence_ref, ObjectType.BLOB))
            report = self.history.objects.get(result.sources_ref, ObjectType.BLOB)
            parse_contract(report, ResearchReport)
            state = AgentState(
                goal=self.scope.goal, working_memory={e.source.id: e for e in evidence.evidence}
            )
            assessment = validate_report(
                state,
                {
                    "sources.json": report,
                    "report.md": self.history.objects.get(result.report_ref, ObjectType.BLOB),
                },
                self.corpus,
            )
            if not assessment.passed:
                raise AgentGitError(ErrorCode.VALIDATION_FAILED, "协作报告未通过独立固定验收")
            return result
        raise AgentGitError(ErrorCode.FORBIDDEN, "不支持的协作消息导出")

    def inbox(self) -> list[tuple[dict[str, Any], CollaborationMessage, Any]]:
        with self.history.engine.connect() as c:
            table = TABLES["collaboration_messages"]
            rows = (
                c.execute(
                    sa.select(table)
                    .where(
                        table.c.repository_id == self.lease.repository_id,
                        table.c.collaboration_id == self.group_id,
                        table.c.receiver_run_id == self.lease.run_id,
                    )
                    .order_by(table.c.sent_seq, table.c.id)
                )
                .mappings()
                .all()
            )
        result = []
        for row in rows:
            envelope = parse_contract(
                self.history.objects.get(row["envelope_ref"], ObjectType.BLOB), CollaborationMessage
            )
            authorize_message(self.program, self.scope.bindings, envelope)
            if (
                envelope.message_id,
                envelope.sender,
                envelope.receiver,
                envelope.task_id,
                envelope.payload_ref,
            ) != (
                str(row["id"]),
                str(row["sender_run_id"]),
                str(self.lease.run_id),
                self.task.task_id,
                row["payload_ref"],
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "收件箱信封与实际消息索引不符")
            self._parent(envelope.parent_event_id, envelope.payload_ref)
            payload = self.validate_payload(envelope.kind, envelope.payload_ref, self.payload.role)
            result.append((dict(row), envelope, payload))
        return result

    def receive(self, state: CollaborationRoleState) -> CollaborationRoleState:
        state = self.load()
        messages = [m for m in self.inbox() if m[1].message_id not in state.received]
        if not messages:
            return state
        events = tuple(
            EventInput(
                "collaboration.message_received",
                "collaboration",
                {"message_id": env.message_id},
                caused_by_run_id=row["sender_run_id"],
                caused_by_seq=row["sent_seq"],
            )
            for row, env, _ in messages
        )
        updated = evolve(
            state, received=(*state.received, *(env.message_id for _, env, _ in messages))
        )

        def project(c: Connection) -> None:
            current = self.history._run(c, self.lease.repository_id, self.lease.run_id)
            for index, (row, _, _) in enumerate(messages):
                c.execute(
                    TABLES["collaboration_receipts"]
                    .insert()
                    .values(
                        message_id=row["id"],
                        repository_id=self.lease.repository_id,
                        receiver_run_id=self.lease.run_id,
                        received_commit_id=current["checkpoint_commit_id"],
                        received_seq=current["last_event_seq"] - len(messages) + index,
                    )
                )

        self.save(updated, "collaboration.inbox_observed", events=events, projection=project)
        return updated

    def head(self, path: SharedPath) -> SharedArtifactHead | None:
        with self.history.engine.connect() as c:
            table = TABLES["collaboration_publications"]
            row = (
                c.execute(
                    sa.select(table)
                    .where(
                        table.c.repository_id == self.lease.repository_id,
                        table.c.collaboration_id == self.group_id,
                        table.c.path == path,
                    )
                    .order_by(table.c.version.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        head = parse_contract(
            self.history.objects.get(row["head_ref"], ObjectType.BLOB), SharedArtifactHead
        )
        if (
            head.repository_id,
            head.collaboration_id,
            head.path,
            head.version,
            head.object_ref,
        ) != (
            str(self.lease.repository_id),
            str(self.group_id),
            path,
            row["version"],
            row["object_ref"],
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "共享版本索引与内容不符")
        return head

    def batch(
        self,
        state: CollaborationRoleState,
        *,
        sends: tuple[tuple[CollaborationRole, MessageKind, str], ...],
        publications: tuple[tuple[SharedPath, int, str], ...],
        status: RunStatus,
        phase: Any,
    ) -> CollaborationRoleState:
        address = self.address()
        if address.agent_run_id != str(self.lease.run_id):
            raise AgentGitError(ErrorCode.FORBIDDEN, "发送父身份必须属于当前租约 Run")
        envelopes = []
        heads = []
        for receiver, kind, ref in sends:
            target = next(a for a in self.scope.bindings.agents if a.role == receiver)
            identifier = uuid5(self.group_id, f"{self.payload.role.value}:{kind}:{receiver.value}")
            envelope = CollaborationMessage(
                repository_id=str(self.lease.repository_id),
                collaboration_id=str(self.group_id),
                message_id=str(identifier),
                sender=str(self.lease.run_id),
                receiver=target.agent_run_id,
                task_id=target.task_id,
                correlation_id=str(self.group_id),
                kind=kind,
                payload_ref=ref,
                parent_event_id=address,
            )
            authorize_message(self.program, self.scope.bindings, envelope)
            self._parent(address, ref)
            self.validate_payload(kind, ref, receiver)
            envelopes.append(envelope)
        current = self.load()
        replayed = self._published_batch(envelopes, publications, status, phase)
        if replayed:
            return current
        if current != state:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "发送状态已被当前角色推进")
        for path, expected, ref in publications:
            parent = self._parent(address, ref)
            if parent.exports.get(path) != ref:
                raise AgentGitError(ErrorCode.FORBIDDEN, "共享值不是当前角色明确导出的路径")
            if path in ("evidence.json", "verification.json"):
                self.validate_payload(
                    "evidence" if path == "evidence.json" else "verification", ref
                )
            heads.append(
                advance_shared_artifact(
                    self.program,
                    self.scope.bindings,
                    self.head(path),
                    path=path,
                    expected_version=expected,
                    object_ref=ref,
                    publisher_event=address,
                )
            )
        events = tuple(
            EventInput(
                "collaboration.message_sent",
                "collaboration",
                {"message_id": e.message_id},
                caused_by_seq=address.seq,
            )
            for e in envelopes
        ) + tuple(
            EventInput(
                "collaboration.shared_published",
                "collaboration",
                {"path": h.path, "version": h.version},
                caused_by_seq=address.seq,
            )
            for h in heads
        )
        updated = evolve(
            state,
            sent=(*state.sent, *(e.message_id for e in envelopes)),
            publications=(*state.publications, *heads),
            status=status,
            phase=phase,
        )

        def project(c: Connection) -> None:
            groups = TABLES["collaborations"]
            group = (
                c.execute(
                    sa.select(groups)
                    .where(
                        groups.c.repository_id == self.lease.repository_id,
                        groups.c.id == self.group_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if group["status"] != "running":
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "协作组当前不允许发送或发布")
            current = self.history._run(c, self.lease.repository_id, self.lease.run_id)
            count = c.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["collaboration_messages"])
                .where(TABLES["collaboration_messages"].c.collaboration_id == self.group_id)
            ).scalar_one()
            if count + len(envelopes) > 32:
                raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "协作消息超过固定上限")
            for index, envelope in enumerate(envelopes):
                obj = self.history.objects.put_json(
                    ObjectType.BLOB, envelope.model_dump(mode="json")
                )
                self.history._ref(
                    c,
                    self.lease.repository_id,
                    obj,
                    "collaboration_message",
                    envelope.message_id,
                    "envelope",
                )
                payload = self.history.objects.put(
                    ObjectType.BLOB, self.history.objects.get(envelope.payload_ref, ObjectType.BLOB)
                )
                self.history._ref(
                    c,
                    self.lease.repository_id,
                    payload,
                    "collaboration_message",
                    envelope.message_id,
                    "payload",
                )
                c.execute(
                    TABLES["collaboration_messages"]
                    .insert()
                    .values(
                        id=UUID(envelope.message_id),
                        repository_id=self.lease.repository_id,
                        collaboration_id=self.group_id,
                        sender_run_id=self.lease.run_id,
                        receiver_run_id=UUID(envelope.receiver),
                        receiver_task_id=UUID(envelope.task_id),
                        envelope_ref=obj.id,
                        payload_ref=payload.id,
                        sent_commit_id=current["checkpoint_commit_id"],
                        sent_seq=current["last_event_seq"] - len(events) + index,
                    )
                )
            for index, head in enumerate(heads):
                table = TABLES["collaboration_publications"]
                latest = c.execute(
                    sa.select(sa.func.coalesce(sa.func.max(table.c.version), 0)).where(
                        table.c.repository_id == self.lease.repository_id,
                        table.c.collaboration_id == self.group_id,
                        table.c.path == head.path,
                    )
                ).scalar_one()
                if latest != head.version - 1:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "共享版本已由另一发布者推进")
                obj = self.history.objects.put_json(ObjectType.BLOB, head.model_dump(mode="json"))
                self.history._ref(
                    c,
                    self.lease.repository_id,
                    obj,
                    "collaboration_share",
                    str(self.group_id),
                    f"head:{head.path}:{head.version}",
                )
                value = self.history.objects.put(
                    ObjectType.BLOB, self.history.objects.get(head.object_ref, ObjectType.BLOB)
                )
                self.history._ref(
                    c,
                    self.lease.repository_id,
                    value,
                    "collaboration_share",
                    str(self.group_id),
                    f"object:{head.path}:{head.version}",
                )
                c.execute(
                    table.insert().values(
                        repository_id=self.lease.repository_id,
                        collaboration_id=self.group_id,
                        path=head.path,
                        version=head.version,
                        publisher_run_id=self.lease.run_id,
                        commit_id=current["checkpoint_commit_id"],
                        event_seq=current["last_event_seq"] - len(events) + len(envelopes) + index,
                        head_ref=obj.id,
                        object_ref=head.object_ref,
                    )
                )
            if status == RunStatus.COMPLETED and self.payload.role == CollaborationRole.SUPERVISOR:
                c.execute(
                    groups.update().where(groups.c.id == self.group_id).values(status="completed")
                )
            wake_collaboration(self.queue, c, self.lease.repository_id, self.group_id)
            self.history.fault("collaboration.exchange_projected")

        self.save(updated, "collaboration.role_transition", events=events, projection=project)
        self.history.fault("collaboration.exchange_committed")
        return updated

    def _published_batch(
        self,
        envelopes: list[CollaborationMessage],
        publications: tuple[tuple[SharedPath, int, str], ...],
        status: RunStatus,
        phase: Any,
    ) -> bool:
        """原子批次成功后的重交只核对旧收据，不再发送、发布或覆盖检查点。"""
        messages, shared = TABLES["collaboration_messages"], TABLES["collaboration_publications"]
        commits = set()
        matched = 0
        with self.history.engine.begin() as c:
            self.queue.guard(c, self.lease, self.lease.run_id)
            for envelope in envelopes:
                row = (
                    c.execute(sa.select(messages).where(messages.c.id == UUID(envelope.message_id)))
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                matched += 1
                if self.history.objects.get(row["envelope_ref"], ObjectType.BLOB) != encode(
                    envelope.model_dump(mode="json")
                ):
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "消息 ID 已绑定其他完整输入")
                commits.add(row["sent_commit_id"])
            for path, expected, ref in publications:
                row = (
                    c.execute(
                        sa.select(shared).where(
                            shared.c.repository_id == self.lease.repository_id,
                            shared.c.collaboration_id == self.group_id,
                            shared.c.path == path,
                            shared.c.version == expected + 1,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None or row["publisher_run_id"] != self.lease.run_id:
                    continue
                matched += 1
                if row["object_ref"] != ref:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "共享版本已绑定其他对象")
                commits.add(row["commit_id"])
        if not matched:
            return False
        if matched != len(envelopes) + len(publications) or len(commits) != 1:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "重复提交与原子消息/共享批次不符")
        original = parse_contract(
            encode(self.history.restore(self.lease.repository_id, commits.pop()).data),
            CollaborationRoleState,
        )
        if (
            original.status != status
            or original.phase != phase
            or (
                original.sent != tuple(e.message_id for e in envelopes)
                or tuple((h.path, h.version - 1, h.object_ref) for h in original.publications)
                != publications
            )
            or any(
                not any(
                    h.path == path
                    and h.version == expected + 1
                    and h.object_ref == ref
                    and h.publisher_event == self.address()
                    for h in original.publications
                )
                for path, expected, ref in publications
            )
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "重交的转换或共享来源与原批次不符")
        return True
