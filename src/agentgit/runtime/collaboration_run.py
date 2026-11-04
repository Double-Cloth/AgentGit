"""受限四角色真实 Worker；模型/工具沿用耐久意图、结果和安全点。"""

import hashlib
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ReadArguments, SearchArguments, ToolRegistry, WriteArguments
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.collaboration import CollaborationRole, MessageKind, TaskKey
from agentgit.core.collaboration_execution import (
    DispatchExport,
    EvidenceExport,
    PendingRoleCall,
    ResultExport,
    RoleDecision,
    RoleModelRequest,
    VerificationExport,
)
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import EffectClass, RunStatus, parse_contract
from agentgit.core.research import AgentConfig, AgentState, Evidence, Source, evolve
from agentgit.runtime.ports import ModelAdapter
from agentgit.runtime.validation import render_report, report_from_memory, validate_report
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.collaboration_control import wake_stopped
from agentgit.services.collaboration_exchange import TERMINAL, CollaborationExchange
from agentgit.services.history import TABLES, EventInput, HistoryService
from agentgit.services.jobs import JobService


class CollaborationRoleRunner:
    def __init__(
        self,
        queue: JobService,
        lease: Lease,
        *,
        fault: Callable[[str], None],
        model_factory: Callable[[str], ModelAdapter] | None = None,
    ):
        self.queue, self.lease, self.fault = queue, lease, fault
        self.exchange = CollaborationExchange(queue, lease, fault=fault)
        self.scope = self.exchange.scope
        if self.exchange.payload.recording_ref is not None:
            raise AgentGitError(
                ErrorCode.FORBIDDEN, "Recorded 角色不能落入 Live 模型或工具构造路径"
            )
        self.model = (
            model_factory(self.scope.policy.role.value)
            if model_factory
            else MockCollaborationModel()
        )
        if self.model.signature != MockCollaborationModel.signature:
            raise AgentGitError(ErrorCode.FORBIDDEN, "首批协作仅允许固定离线角色模型")
        limits = self.scope.policy.limits
        parent = queue.research.data_root / "collaboration" / str(lease.run_id)
        parent.mkdir(parents=True, exist_ok=True)
        self.workspace = ArtifactWorkspace(
            parent / uuid4().hex,
            max_file_bytes=limits.max_artifact_bytes,
            max_files=limits.max_files,
        )
        names = tuple(
            p
            for p in self.scope.policy.permissions
            if p in {"corpus_search", "source_read", "artifact_write"}
        )
        self.tools = (
            ToolRegistry(
                self.exchange.corpus, self.workspace, AgentConfig(tools=names, limits=limits)
            )
            if names
            else None
        )
        rules = [
            ToolRule(
                provider="llm:collaboration-role/v1",
                signature_digest=self.model.signature,
                effect=EffectClass.READ_ONLY,
                scope="local",
                preauthorized=True,
            )
        ]
        if self.tools is not None:
            rules.extend(
                ToolRule(
                    provider=f"tool:{n}/v1",
                    signature_digest=self.tools.signature(n),
                    effect=self.tools.effect(n),
                    scope="local",
                    preauthorized=True,
                )
                for n in names
            )
        job = queue.get(lease.repository_id, lease.job_id)
        self.exchange.environment = evolve(
            self.scope.environment,
            input_ref=job["payload_ref"],
            model_signature=self.model.signature,
            tool_signatures=tuple(r.signature_digest for r in rules),
            prompt_digest=hashlib.sha256(
                encode(
                    dict(
                        role=self.scope.policy.role.value,
                        private_input_ref=self.exchange.payload.private_input_ref,
                        goal=self.scope.goal.model_dump(mode="json"),
                    )
                )
            ).hexdigest(),
        )
        self.state = self.exchange.load()
        self.workspace.replace(self.exchange.files, self.exchange.media)

        def guard(c: Connection, r: Any) -> None:
            queue.guard(c, lease, r)

        self.history = HistoryService(
            queue.history.engine,
            queue.history.objects,
            lease_guard=guard,
            fault=lambda p: fault("collaboration.call." + p),
        )
        self.gateway = RecordedCallGateway(
            self.history,
            lease.repository_id,
            lease.run_id,
            self._checkpoint,
            authorization=ApprovalService(
                self.history, ToolPolicy(rules=tuple(rules)), actor="collaboration-runtime"
            ),
        )

    def _checkpoint(self) -> tuple[str | None, int]:
        run = self.queue.run(self.lease.repository_id, self.lease.run_id)
        return run["checkpoint_commit_id"], self.history.branch(
            self.lease.repository_id, run["branch_name"]
        )["version"]

    def _save(
        self,
        event: str,
        *,
        details: dict[str, Any] | None = None,
        projection: Callable[[Connection], None] | None = None,
        apply: bool = False,
        events: tuple[EventInput, ...] = (),
    ) -> None:
        self.exchange.save(
            self.state,
            event,
            details=details,
            projection=projection,
            apply_control=apply,
            events=events,
        )
        self.fault("collaboration." + event.rsplit(".", 1)[-1] + "_saved")

    def _stop_reason(self) -> str | None:
        control = self.queue.apply_control(self.lease)
        if control == "cancel":
            return "cancel"
        with self.history.engine.connect() as c:
            group = c.execute(
                sa.select(TABLES["collaborations"].c.status).where(
                    TABLES["collaborations"].c.id == self.exchange.group_id
                )
            ).scalar_one()
        if group in TERMINAL:
            return "cancel"
        if control == "budget_exhausted":
            return control
        return "pause" if group == "paused" or control == "pause" else None

    def _check(self) -> None:
        reason = self._stop_reason()
        if reason:
            raise AgentGitError(ErrorCode.CONTROL_REQUESTED, reason)
        if not self._ready():
            raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "dependencies")

    def _ready(self) -> bool:
        role = self.state.role.value
        if role == "supervisor" and not self.state.sent:
            return True
        dependencies = {
            "supervisor": ("writer",),
            "researcher": (),
            "verifier": ("researcher",),
            "writer": ("researcher", "verifier"),
        }[role]
        with self.history.engine.connect() as c:
            tasks, runs = TABLES["collaboration_tasks"], TABLES["runs"]
            rows = (
                c.execute(
                    sa.select(tasks.c.role, runs.c.status)
                    .join(
                        runs,
                        sa.and_(
                            runs.c.repository_id == tasks.c.repository_id,
                            runs.c.id == tasks.c.run_id,
                        ),
                    )
                    .where(
                        tasks.c.repository_id == self.lease.repository_id,
                        tasks.c.collaboration_id == self.exchange.group_id,
                    )
                )
                .mappings()
                .all()
            )
        statuses = {r["role"]: r["status"] for r in rows}
        if any(statuses.get(dep) != "completed" for dep in dependencies):
            return False
        kinds = {e.kind for _, e, _ in self.exchange.inbox()}
        return {
            "supervisor": {"result"},
            "researcher": {"dispatch"},
            "verifier": {"dispatch", "evidence"},
            "writer": {"dispatch", "evidence", "verification"},
        }[role] <= kinds

    def _call(
        self, category: Literal["model", "tool"], name: str, step: str, request: bytes
    ) -> bytes:
        provider = "llm:collaboration-role/v1" if category == "model" else f"tool:{name}/v1"
        if category == "model":
            signature, effect = self.model.signature, EffectClass.READ_ONLY
        else:
            if self.tools is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "当前角色没有工具权限")
            signature, effect = self.tools.signature(name), self.tools.effect(name)
        existing = next((t for t in self.state.traces if t.logical_step_id == step), None)
        if existing is not None:
            if (existing.provider, existing.signature_digest) != (
                provider,
                signature,
            ) or self.history.objects.get(existing.request_ref, ObjectType.BLOB) != request:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "保存调用与当前角色动作/输入不符")
            return self.history.objects.get(existing.response_ref, ObjectType.BLOB)
        limits = self.scope.policy.limits
        if self.state.pending is None:
            self._check()
            bound = (
                min(1024, limits.max_response_bytes)
                if category == "model"
                else limits.max_response_bytes
            )
            obj = self.history.objects.put(ObjectType.BLOB, request)
            reserved = PendingRoleCall(
                category=category,
                name=name,
                step=step,
                provider=provider,
                signature=signature,
                effect=effect,
                request_ref=obj.id,
                max_bytes=bound,
            )
            usage = evolve(
                self.state.usage,
                steps=self.state.usage.steps + 1,
                model_calls=self.state.usage.model_calls + int(category == "model"),
                tool_calls=self.state.usage.tool_calls + int(category == "tool"),
                reserved_tokens=len(request) + bound if category == "model" else 0,
            )
            candidate = evolve(self.state, pending=reserved, usage=usage)
            self.exchange.save(candidate, "collaboration.call_reserved")
            self.state = candidate
            self.fault("collaboration.call_reserved")
        pending = self.state.pending
        assert pending is not None
        if (
            (pending.provider, pending.signature, pending.effect) != (provider, signature, effect)
            or pending.step != step
            or pending.category != category
            or pending.name != name
            or self.history.objects.get(pending.request_ref, ObjectType.BLOB) != request
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "角色未决调用与当前固定动作不符")
        kwargs: dict[str, Any] = dict(
            step=step,
            attempt=1,
            provider=pending.provider,
            signature=pending.signature,
            effect=pending.effect,
            request=request,
            max_bytes=pending.max_bytes,
        )
        outcome = self.gateway.recorded(**kwargs)
        if outcome is None:
            self._check()
            if self.state.status == RunStatus.PAUSED:
                self.state = evolve(self.state, status=RunStatus.RUNNING, error_code=None)
                self._save(
                    "collaboration.resumed", apply=self.queue.apply_control(self.lease) == "resume"
                )

            def invoke() -> bytes:
                # 意图已提交后属于在途调用；仅复核租约，控制在结果结算后落实。
                with self.history.engine.begin() as c:
                    self.queue.guard(c, self.lease, self.lease.run_id)
                self.fault("collaboration.after_call_intent")
                if category == "model":
                    return self.model.invoke(request)
                if self.tools is None:
                    raise AgentGitError(ErrorCode.FORBIDDEN, "角色未获得工具能力")
                return self.tools.invoke(name, decode(request))

            outcome = self.gateway.call(**kwargs, invoke=invoke)
        self.fault("collaboration.after_saved_result")
        usage = evolve(
            self.state.usage,
            tokens=self.state.usage.tokens
            + (
                len(request)
                + (len(outcome.payload) if outcome.error is None else outcome.output_bytes)
                if category == "model"
                else 0
            ),
            reserved_tokens=0,
        )
        self.state = evolve(
            self.state, pending=None, usage=usage, traces=(*self.state.traces, outcome.trace)
        )
        self._save("collaboration.call_observed")
        if outcome.error is not None:
            raise outcome.error
        return outcome.payload

    def _decision(self, action: Any) -> None:
        if any(d.action == action for d in self.state.decisions):
            return
        request = self._model_request(action)
        raw = encode(request.model_dump(mode="json"))
        if len(raw) > self.scope.policy.limits.max_context_bytes:
            raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "角色模型上下文超过独立上限")
        decision = parse_contract(
            self._call("model", "decision", "model-" + action, raw), RoleDecision
        )
        if decision.role != self.state.role or decision.action != action:
            raise AgentGitError(ErrorCode.FORBIDDEN, "模型不能更换角色或固定任务")
        self.state = evolve(self.state, decisions=(*self.state.decisions, decision), phase="work")
        self._save("collaboration.decision_observed")

    def _model_request(self, action: Any) -> RoleModelRequest:
        inbox = {e.message_id: (e, p) for _, e, p in self.exchange.inbox()}
        selected = [inbox[identifier] for identifier in self.state.received]
        return RoleModelRequest(
            role=self.state.role,
            action=action,
            goal=self.scope.goal,
            private_notes=self.scope.private_input.notes,
            inbox_payloads=tuple(e.payload_ref for e, _ in selected),
            inbox=tuple(p for _, p in selected),
        )

    def _blob(self, value: Any) -> str:
        return self.history.objects.put_json(ObjectType.BLOB, value.model_dump(mode="json")).id

    def _ready_exports(self, exports: dict[str, str]) -> None:
        candidate = evolve(self.state, exports=exports, phase="export_ready")
        self.exchange.save(candidate, "collaboration.export_ready")
        self.state = candidate
        self.fault("collaboration.export_ready_saved")

    def _dispatch(self) -> None:
        self._decision("delegate")
        assignments: tuple[tuple[CollaborationRole, TaskKey], ...] = (
            (CollaborationRole.RESEARCHER, "collect"),
            (CollaborationRole.VERIFIER, "verify"),
            (CollaborationRole.WRITER, "write"),
        )
        if self.state.phase != "export_ready":
            self._ready_exports(
                {
                    "message:" + task: self._blob(
                        DispatchExport(program_ref=self.state.program_ref, task=task)
                    )
                    for _, task in assignments
                }
            )
        sends: tuple[tuple[CollaborationRole, MessageKind, str], ...] = tuple(
            (role, "dispatch", self.state.exports["message:" + task]) for role, task in assignments
        )
        self._check()
        self.state = self.exchange.batch(
            self.state, sends=sends, publications=(), status=RunStatus.PAUSED, phase="await_result"
        )

    def _collect(self) -> None:
        self._decision("collect")
        if not self.state.source_ids:
            reply = decode(
                self._call(
                    "tool",
                    "corpus_search",
                    "search",
                    encode(
                        SearchArguments(frameworks=self.scope.goal.frameworks).model_dump(
                            mode="json"
                        )
                    ),
                )
            )
            if not isinstance(reply, dict) or reply.get("source_ids") != list(
                self.exchange.corpus.search(self.scope.goal.frameworks)
            ):
                raise AgentGitError(ErrorCode.TOOL_ERROR, "搜索结果不符冻结语料")
            self.state = evolve(self.state, source_ids=tuple(reply["source_ids"]))
            self._save("collaboration.sources_selected")
        for index, source_id in enumerate(self.state.source_ids):
            if any(e.source.id == source_id for e in self.state.evidence):
                continue
            reply = decode(
                self._call(
                    "tool",
                    "source_read",
                    f"read-{index}",
                    encode(ReadArguments(source_id=source_id).model_dump(mode="json")),
                )
            )
            source = parse_contract(encode(reply["source"]), Source)
            if source != self.exchange.corpus.read(source_id):
                raise AgentGitError(ErrorCode.TOOL_ERROR, "读取来源不符冻结字节")
            self.state = evolve(
                self.state,
                evidence=(
                    *self.state.evidence,
                    Evidence(source=source, logical_step_id=f"read-{index}", attempt=1),
                ),
            )
            self._save("collaboration.source_observed")
        if self.state.phase != "export_ready":
            evidence = self._blob(EvidenceExport(evidence=self.state.evidence))
            self._ready_exports({"evidence.json": evidence, "message": evidence})
        self._check()
        self.state = self.exchange.batch(
            self.state,
            sends=(
                (CollaborationRole.VERIFIER, "evidence", self.state.exports["message"]),
                (CollaborationRole.WRITER, "evidence", self.state.exports["message"]),
            ),
            publications=(("evidence.json", 0, self.state.exports["evidence.json"]),),
            status=RunStatus.COMPLETED,
            phase="done",
        )

    def _incoming(self, kind: str) -> tuple[str, Any]:
        values = [(e.payload_ref, p) for _, e, p in self.exchange.inbox() if e.kind == kind]
        if len(values) != 1:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "角色缺少唯一的声明输入消息")
        return values[0]

    def _verify(self) -> None:
        self._decision("verify")
        ref, evidence = self._incoming("evidence")
        for index, item in enumerate(evidence.evidence):
            reply = decode(
                self._call(
                    "tool",
                    "source_read",
                    f"read-{index}",
                    encode(ReadArguments(source_id=item.source.id).model_dump(mode="json")),
                )
            )
            if parse_contract(encode(reply["source"]), Source) != item.source:
                raise AgentGitError(ErrorCode.VALIDATION_FAILED, "Verifier 独立读取与导出证据不符")
        verification = VerificationExport(
            evidence_ref=ref,
            source_ids=tuple(e.source.id for e in evidence.evidence),
            evaluation_time=self.scope.environment.evaluation_time,
        )
        verification_ref = self._blob(verification)
        self.exchange.validate_payload("verification", verification_ref)
        if self.state.phase != "export_ready":
            self._ready_exports(
                {
                    "evidence.json": ref,
                    "verification.json": verification_ref,
                    "message": verification_ref,
                }
            )
        self._check()
        self.state = self.exchange.batch(
            self.state,
            sends=((CollaborationRole.WRITER, "verification", verification_ref),),
            publications=(("evidence.json", 1, ref), ("verification.json", 0, verification_ref)),
            status=RunStatus.COMPLETED,
            phase="done",
        )

    def _write(self) -> None:
        self._decision("write")
        evidence_ref, evidence = self._incoming("evidence")
        verification_ref, verification = self._incoming("verification")
        if verification.evidence_ref != evidence_ref:
            raise AgentGitError(ErrorCode.VALIDATION_FAILED, "Writer 输入证据与验证不符")
        memory = AgentState(
            goal=self.scope.goal, working_memory={e.source.id: e for e in evidence.evidence}
        )
        report = report_from_memory(memory, self.scope.environment.evaluation_time)
        outputs = {
            "report.md": (render_report(report), "text/markdown"),
            "sources.json": (encode(report.model_dump(mode="json")).decode(), "application/json"),
        }
        for index, (path, (text, media)) in enumerate(outputs.items()):
            prospective = {**self.workspace.contents(), path: text.encode()}
            limits = self.scope.policy.limits
            if (
                len(prospective) > limits.max_files
                or sum(map(len, prospective.values())) > limits.max_artifact_bytes
            ):
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "写入前角色产物总预算已耗尽")
            request = encode(
                WriteArguments(path=path, text=text, media_type=media).model_dump(mode="json")
            )
            self._call("tool", "artifact_write", f"write-{index}", request)
            # 已保存结果接管时只恢复固定输出字节，不重新调用工具。
            self.workspace.write(path, text, media)
            ref = self.history.objects.put(ObjectType.BLOB, text.encode()).id
            if self.state.exports.get(path) != ref:
                candidate = evolve(self.state, exports={**self.state.exports, path: ref})
                self.exchange.save(candidate, "collaboration.artifact_observed")
                self.state = candidate
        if not validate_report(memory, self.workspace.contents(), self.exchange.corpus).passed:
            raise AgentGitError(ErrorCode.VALIDATION_FAILED, "Writer 报告未通过固定验收")
        result = ResultExport(
            evidence_ref=evidence_ref,
            verification_ref=verification_ref,
            report_ref=self.state.exports["report.md"],
            sources_ref=self.state.exports["sources.json"],
        )
        if self.state.phase != "export_ready":
            self._ready_exports({**self.state.exports, "message": self._blob(result)})
        self._check()
        self.state = self.exchange.batch(
            self.state,
            sends=((CollaborationRole.SUPERVISOR, "result", self.state.exports["message"]),),
            publications=(
                ("report.md", 0, result.report_ref),
                ("sources.json", 0, result.sources_ref),
            ),
            status=RunStatus.COMPLETED,
            phase="done",
        )

    def _complete(self) -> None:
        self._decision("complete")
        self._incoming("result")
        self._check()
        self.state = evolve(self.state, status=RunStatus.COMPLETED, phase="done")

        def project(c: Connection) -> None:
            c.execute(
                TABLES["collaborations"]
                .update()
                .where(TABLES["collaborations"].c.id == self.exchange.group_id)
                .values(status="completed")
            )

        self._save("collaboration.completed", projection=project)

    def _stop(self, status: RunStatus, code: str | None) -> None:
        with self.history.engine.connect() as c:
            cause = (
                c.execute(
                    sa.select(
                        TABLES["collaborations"].c.failure_run_id,
                        TABLES["collaborations"].c.failure_event_seq,
                    ).where(TABLES["collaborations"].c.id == self.exchange.group_id)
                )
                .mappings()
                .one()
            )
        parent_events: tuple[EventInput, ...] = ()
        if cause["failure_run_id"] is not None and cause["failure_run_id"] != self.lease.run_id:
            parent_events = (
                EventInput(
                    "collaboration.parent_stopped",
                    "collaboration",
                    {"collaboration_id": str(self.exchange.group_id)},
                    caused_by_run_id=cause["failure_run_id"],
                    caused_by_seq=cause["failure_event_seq"],
                ),
            )
        unknown = status == RunStatus.OUTCOME_UNKNOWN
        pending = self.state.pending if status == RunStatus.PAUSED or unknown else None
        usage = (
            self.state.usage if pending is not None else evolve(self.state.usage, reserved_tokens=0)
        )
        if pending is None and self.state.pending is not None:
            abandoned = self.state.pending
            with self.history.engine.connect() as c:
                calls = TABLES["external_calls"]
                intent = c.execute(
                    sa.select(calls.c.id).where(
                        calls.c.repository_id == self.lease.repository_id,
                        calls.c.run_id == self.lease.run_id,
                        calls.c.logical_step_id == abandoned.step,
                        calls.c.call_index == 0,
                        calls.c.attempt == 1,
                    )
                ).scalar_one_or_none()
            if intent is None:
                usage = evolve(
                    usage,
                    steps=usage.steps - 1,
                    model_calls=usage.model_calls - int(abandoned.category == "model"),
                    tool_calls=usage.tool_calls - int(abandoned.category == "tool"),
                )
                parent_events = (
                    *parent_events,
                    EventInput(
                        "collaboration.call_aborted",
                        "collaboration",
                        dict(step=abandoned.step, category=abandoned.category, reason="no_intent"),
                    ),
                )
        self.state = evolve(
            self.state,
            status=status,
            phase=self.state.phase if pending is not None or status == RunStatus.PAUSED else "done",
            pending=pending,
            usage=usage,
            error_code=code,
        )

        def project(c: Connection) -> None:
            if status in {
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.BUDGET_EXHAUSTED,
                RunStatus.OUTCOME_UNKNOWN,
            }:
                groups = TABLES["collaborations"]
                row = c.execute(
                    sa.select(groups.c.status)
                    .where(groups.c.id == self.exchange.group_id)
                    .with_for_update()
                ).scalar_one()
                if row not in TERMINAL:
                    current = self.history._run(c, self.lease.repository_id, self.lease.run_id)
                    c.execute(
                        groups.update()
                        .where(groups.c.id == self.exchange.group_id)
                        .values(
                            status=status.value,
                            failure_run_id=self.lease.run_id,
                            failure_event_seq=current["last_event_seq"],
                        )
                    )
                wake_stopped(c, self.lease.repository_id, self.exchange.group_id)

        self._save(
            "collaboration.stopped",
            projection=project,
            apply=self.queue.apply_control(self.lease) is not None,
            events=parent_events,
        )

    def run(self) -> dict[str, Any]:
        try:
            if self.state.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.BUDGET_EXHAUSTED,
            }:
                self.queue.finish(self.lease, self.state.status.value)
                return self.summary()
            if self.state.pending is not None:
                pending = self.state.pending
                if pending.category == "model":
                    request = parse_contract(
                        self.history.objects.get(pending.request_ref, ObjectType.BLOB),
                        RoleModelRequest,
                    )
                    allowed = {
                        CollaborationRole.SUPERVISOR: {"delegate", "complete"},
                        CollaborationRole.RESEARCHER: {"collect"},
                        CollaborationRole.VERIFIER: {"verify"},
                        CollaborationRole.WRITER: {"write"},
                    }
                    if request.action not in allowed[
                        self.state.role
                    ] or request != self._model_request(request.action):
                        raise AgentGitError(
                            ErrorCode.REPLAY_MISMATCH, "未决模型上下文不是当前角色的已接收输入"
                        )
                self._call(
                    pending.category,
                    pending.name,
                    pending.step,
                    self.history.objects.get(pending.request_ref, ObjectType.BLOB),
                )
            self._check()
            current = self.queue.run(self.lease.repository_id, self.lease.run_id)
            if (
                self.state.status != RunStatus.RUNNING
                or current["status"] != "running"
                or self.queue.apply_control(self.lease) == "resume"
            ):
                self.state = evolve(self.state, status=RunStatus.RUNNING, error_code=None)
                self._save(
                    "collaboration.resumed", apply=self.queue.apply_control(self.lease) == "resume"
                )
            self.state = self.exchange.receive(self.state)
            if self.state.role == CollaborationRole.SUPERVISOR:
                if self.state.sent:
                    self._complete()
                else:
                    self._dispatch()
            elif self.state.role == CollaborationRole.RESEARCHER:
                self._collect()
            elif self.state.role == CollaborationRole.VERIFIER:
                self._verify()
            else:
                self._write()
        except AgentGitError as error:
            if error.code == ErrorCode.VERSION_CONFLICT:
                return dict(
                    job_id=str(self.lease.job_id),
                    run_id=str(self.lease.run_id),
                    status="lease_lost",
                    error_code=error.code.value,
                )
            reason = None
            if error.code == ErrorCode.CONTROL_REQUESTED:
                reason = self._stop_reason()
                if reason is None and str(error) in (
                    "dependencies",
                    "pause",
                    "cancel",
                    "budget_exhausted",
                ):
                    reason = str(error)
            status = (
                RunStatus.PAUSED
                if reason in ("pause", "dependencies")
                else RunStatus.CANCELLED
                if reason == "cancel"
                else RunStatus.BUDGET_EXHAUSTED
                if reason == "budget_exhausted" or error.code == ErrorCode.BUDGET_EXHAUSTED
                else RunStatus.OUTCOME_UNKNOWN
                if error.code == ErrorCode.OUTCOME_UNKNOWN
                else RunStatus.FAILED
            )
            self._stop(status, None if reason else error.code.value)
        self.queue.finish(self.lease, self.state.status.value)
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return dict(
            job_id=str(self.lease.job_id),
            run_id=str(self.lease.run_id),
            status=self.queue.run(self.lease.repository_id, self.lease.run_id)["status"],
        )
