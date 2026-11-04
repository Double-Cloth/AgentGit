"""固定四角色的内存 Recorded 重算核心；不构造执行客户端或写入历史。"""

from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import (
    ReadArguments,
    SearchArguments,
    WriteArguments,
    local_tool_signature,
)
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.collaboration import (
    CausalEventAddress,
    CollaborationBindings,
    CollaborationMessage,
    CollaborationProgram,
    MessageKind,
    SharedArtifactHead,
    TaskKey,
    advance_shared_artifact,
    authorize_message,
)
from agentgit.core.collaboration_controls import CollaborationControlFact
from agentgit.core.collaboration_execution import (
    CollaborationRoleState,
    DispatchExport,
    EvidenceExport,
    PendingRoleCall,
    ResultExport,
    RoleAction,
    RoleDecision,
    RoleModelRequest,
    VerificationExport,
)
from agentgit.core.collaboration_runs import RolePrivateInput
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenCorpus
from agentgit.core.models import (
    CallOccurrence,
    EffectClass,
    EnvironmentManifest,
    RunStatus,
    parse_contract,
)
from agentgit.core.research import AgentState, Evidence, Source, evolve
from agentgit.core.snapshots import ArtifactEntry, EventManifest, EventPayload, StateSnapshot
from agentgit.replay.recorded import RecordedAdapter
from agentgit.runtime.validation import render_report, report_from_memory, validate_report
from agentgit.services.collaboration_recordings import CollaborationSource, CollaborationSourceEvent

PAYLOAD_SCHEMAS: dict[str, type[Any]] = dict(
    dispatch=DispatchExport,
    evidence=EvidenceExport,
    verification=VerificationExport,
    result=ResultExport,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, message)


@dataclass(frozen=True)
class CollaborationReplayResult:
    states: tuple[tuple[CollaborationRoleState, ...], ...]
    state_refs: tuple[tuple[str, ...], ...]
    artifacts: tuple[tuple[str, bytes], ...]
    recorded_calls: int
    # 保留源停止点的有限局部转换核对结果，不把它当作新的操作者授权。
    stop_boundaries: tuple[tuple[str, str], ...]


class CollaborationRecordedEngine:
    def __init__(self, source: CollaborationSource):
        self.source, self.index = source, source.index
        self.raw: dict[str, bytes] = {}
        for ref, kind, raw in source.objects:
            require(
                ref not in self.raw and object_digest(kind, raw) == ref,
                "协作重算对象重复或摘要不符",
            )
            self.raw[ref] = raw
        require(
            tuple(sorted(self.raw)) == self.index.object_refs
            and sum(map(len, self.raw.values())) == self.index.total_bytes,
            "协作重算来源闭包不完整",
        )
        self.program = parse_contract(self.read(self.index.program_ref), CollaborationProgram)
        self.bindings = parse_contract(self.read(self.index.bindings_ref), CollaborationBindings)
        self.environment = parse_contract(
            self.read(self.index.environment_ref), EnvironmentManifest
        )
        corpus = parse_contract(self.read(self.index.corpus_ref), FrozenCorpus)
        self.corpus = Corpus(corpus.sources, evaluation_time=corpus.evaluation_time)
        self.roles = {r.run_id: r for r in self.index.roles}
        self.state = {
            r.run_id: CollaborationRoleState(
                collaboration_id=self.index.collaboration_id,
                role=r.role,
                program_ref=self.index.program_ref,
                private_input_ref=r.private_input_ref,
            )
            for r in self.index.roles
        }
        self.adapters = {
            r.run_id: RecordedAdapter(recordings, lambda: None)
            for r, recordings in zip(self.index.roles, source.recordings, strict=True)
        }
        self.messages = {m.message_id: m for m in source.messages}
        require(set(self.messages) == set(self.index.message_ids), "协作重算缺少完整消息输入")
        self.available: dict[str, CollaborationMessage] = {}
        self.shared: dict[str, SharedArtifactHead] = {}
        self.pending_sends: dict[str, list[str]] = {r: [] for r in self.roles}
        self.pending_heads: dict[str, list[SharedArtifactHead]] = {r: [] for r in self.roles}
        self.observations: dict[str, Any] = {}
        self.last_response: dict[str, bytes] = {}
        self.checkpoint: dict[str, str | None] = {r: None for r in self.roles}
        self.export_addresses: dict[str, CausalEventAddress] = {}
        self.started: dict[str, int] = {}
        self.controls: dict[str, str | None] = {r: None for r in self.roles}
        self.group_action = "resume"
        self.revision = 0
        self.facts: dict[tuple[str, int], CollaborationControlFact] = {}
        self.seen: set[tuple[str, int]] = set()
        self.saved: dict[str, list[CollaborationRoleState]] = {r: [] for r in self.roles}
        self.refs: dict[str, list[str]] = {r: [] for r in self.roles}
        self.boundaries: list[tuple[str, str]] = []

    def read(self, ref: str) -> bytes:
        if ref not in self.raw:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "协作重算缺少固定来源字节")
        return self.raw[ref]

    def blob(self, value: Any) -> str:
        raw = encode(value.model_dump(mode="json")) if hasattr(value, "model_dump") else value
        require(isinstance(raw, bytes), "协作重算导出必须为规范字节")
        ref = object_digest(ObjectType.BLOB, raw)
        require(self.read(ref) == raw, "重算导出与来源字节不符")
        return ref

    def incoming(self, run: str, kind: str) -> tuple[str, Any]:
        messages = [
            self.available[m]
            for m in self.state[run].received
            if m in self.available and self.available[m].kind == kind
        ]
        require(len(messages) == 1, "协作重算缺少自身唯一已接收输入")
        message = messages[0]
        return message.payload_ref, parse_contract(
            self.read(message.payload_ref), PAYLOAD_SCHEMAS[kind]
        )

    def ready(self, run: str) -> bool:
        state = self.state[run]
        if state.role.value == "supervisor" and not state.sent:
            return True
        dependencies = {
            "supervisor": ("writer",),
            "researcher": (),
            "verifier": ("researcher",),
            "writer": ("researcher", "verifier"),
        }[state.role.value]
        by_role = {s.role.value: s for s in self.state.values()}
        kinds = {m.kind for m in self.available.values() if m.receiver == run}
        required = {
            "supervisor": {"result"},
            "researcher": {"dispatch"},
            "verifier": {"dispatch", "evidence"},
            "writer": {"dispatch", "evidence", "verification"},
        }[state.role.value]
        return (
            all(by_role[r].status == RunStatus.COMPLETED for r in dependencies)
            and required <= kinds
        )

    def action_allowed(self, run: str) -> None:
        require(
            self.group_action == "resume" and self.controls[run] != "pause" and self.ready(run),
            "协作重算在停止请求或缺依赖后出现新推进",
        )

    def outputs(self, run: str) -> dict[str, tuple[bytes, str]]:
        evidence_ref, evidence = self.incoming(run, "evidence")
        _, verification = self.incoming(run, "verification")
        require(
            verification.evidence_ref == evidence_ref
            and verification.evaluation_time == self.environment.evaluation_time,
            "Writer 重算的验证与证据不符",
        )
        memory = AgentState(
            goal=self.program.goal, working_memory={e.source.id: e for e in evidence.evidence}
        )
        report = report_from_memory(memory, self.environment.evaluation_time)
        outputs = {
            "report.md": (render_report(report).encode(), "text/markdown"),
            "sources.json": (encode(report.model_dump(mode="json")), "application/json"),
        }
        require(
            validate_report(memory, {p: b for p, (b, _) in outputs.items()}, self.corpus).passed,
            "重算报告未通过固定验收",
        )
        return outputs

    def call_spec(self, run: str) -> tuple[PendingRoleCall, bytes]:
        state = self.state[run]
        n, role = len(state.traces), state.role.value
        limits = self.program.policy(state.role).limits
        category: Literal["model", "tool"]
        arguments: SearchArguments | ReadArguments | WriteArguments
        if n == 0 or role == "supervisor":
            action = {
                "supervisor": "delegate" if n == 0 else "complete",
                "researcher": "collect",
                "verifier": "verify",
                "writer": "write",
            }[role]
            require(n == 0 or (role == "supervisor" and n == 1), "模型动作超出固定流程")
            private = parse_contract(self.read(state.private_input_ref), RolePrivateInput)
            inbox = [self.available[m] for m in state.received]
            request = encode(
                RoleModelRequest(
                    role=state.role,
                    action=cast(RoleAction, action),
                    goal=self.program.goal,
                    private_notes=private.notes,
                    inbox_payloads=tuple(m.payload_ref for m in inbox),
                    inbox=tuple(
                        parse_contract(self.read(m.payload_ref), PAYLOAD_SCHEMAS[m.kind])
                        for m in inbox
                    ),
                ).model_dump(mode="json")
            )
            category, name, step, provider, signature, effect, bound = (
                "model",
                "decision",
                "model-" + action,
                "llm:collaboration-role/v1",
                MockCollaborationModel.signature,
                EffectClass.READ_ONLY,
                min(1024, limits.max_response_bytes),
            )
        else:
            require(bool(state.decisions), "工具不能先于角色决策")
            category = "tool"
            if role == "researcher" and n == 1:
                name, step = "corpus_search", "search"
                arguments = SearchArguments(frameworks=self.program.goal.frameworks)
            elif role in ("researcher", "verifier"):
                source_ids = (
                    state.source_ids
                    if role == "researcher"
                    else tuple(e.source.id for e in self.incoming(run, "evidence")[1].evidence)
                )
                index = n - (2 if role == "researcher" else 1)
                require(0 <= index < len(source_ids), "读取动作超出独立来源范围")
                name, step = "source_read", f"read-{index}"
                arguments = ReadArguments(source_id=source_ids[index])
            else:
                require(role == "writer" and n in (1, 2), "工具动作超出固定角色流程")
                path = ("report.md", "sources.json")[n - 1]
                text, media = self.outputs(run)[path]
                name, step = "artifact_write", f"write-{n - 1}"
                arguments = WriteArguments(path=path, text=text.decode(), media_type=media)
            require(name in self.program.policy(state.role).permissions, "重算工具未获当前角色权限")
            request = encode(arguments.model_dump(mode="json"))
            provider, signature, bound = (
                f"tool:{name}/v1",
                local_tool_signature(name),
                limits.max_response_bytes,
            )
            effect = (
                EffectClass.IDEMPOTENT_WRITE if name == "artifact_write" else EffectClass.READ_ONLY
            )
        return PendingRoleCall(
            category=category,
            name=name,
            step=step,
            provider=provider,
            signature=signature,
            effect=effect,
            request_ref=object_digest(ObjectType.BLOB, request),
            max_bytes=bound,
        ), request

    def local(self, run: str, state: CollaborationRoleState) -> CollaborationRoleState:
        """调用观测后，首个可能被控制挡住的纯内存转换。"""
        if not state.traces:
            return state
        trace = state.traces[-1]
        raw = self.last_response[run]
        if trace.provider.startswith("llm:"):
            decision = parse_contract(raw, RoleDecision)
            require(
                decision.role == state.role and trace.logical_step_id == "model-" + decision.action,
                "录制模型响应更换了固定角色或动作",
            )
            if decision not in state.decisions:
                return evolve(state, decisions=(*state.decisions, decision), phase="work")
        elif state.role.value == "researcher":
            if trace.logical_step_id == "search" and not state.source_ids:
                reply = decode(raw)
                require(
                    reply
                    == dict(
                        schema_version=1,
                        source_ids=list(self.corpus.search(self.program.goal.frameworks)),
                    ),
                    "重算搜索响应与冻结语料不符",
                )
                return evolve(state, source_ids=tuple(reply["source_ids"]))
            if trace.logical_step_id.startswith("read-"):
                index = int(trace.logical_step_id.removeprefix("read-"))
                source = parse_contract(encode(decode(raw)["source"]), Source)
                require(
                    source == self.corpus.read(state.source_ids[index]), "重算读取响应不是冻结来源"
                )
                if not any(e.source.id == source.id for e in state.evidence):
                    return evolve(
                        state,
                        evidence=(
                            *state.evidence,
                            Evidence(
                                source=source, logical_step_id=trace.logical_step_id, attempt=1
                            ),
                        ),
                    )
        if state.role.value == "supervisor" and len(state.decisions) == 2 and state.phase == "work":
            self.incoming(run, "result")
            return evolve(state, status=RunStatus.COMPLETED, phase="done")
        return state

    def export(self, run: str) -> dict[str, str]:
        state, role = self.state[run], self.state[run].role.value
        if role == "supervisor":
            return {
                "message:" + task: self.blob(
                    DispatchExport(program_ref=self.index.program_ref, task=cast(TaskKey, task))
                )
                for task in ("collect", "verify", "write")
            }
        if role == "researcher":
            require(len(state.evidence) == 6, "Researcher 重算缺少完整证据")
            ref = self.blob(EvidenceExport(evidence=state.evidence))
            return {"evidence.json": ref, "message": ref}
        evidence_ref, evidence = self.incoming(run, "evidence")
        if role == "verifier":
            require(
                len(state.traces) == 7
                and tuple(e.source.id for e in evidence.evidence)
                == self.corpus.search(self.program.goal.frameworks),
                "Verifier 未独立读完全部来源",
            )
            verified = VerificationExport(
                evidence_ref=evidence_ref,
                source_ids=tuple(e.source.id for e in evidence.evidence),
                evaluation_time=self.environment.evaluation_time,
            )
            ref = self.blob(verified)
            return {"evidence.json": evidence_ref, "verification.json": ref, "message": ref}
        verification_ref, _ = self.incoming(run, "verification")
        result = ResultExport(
            evidence_ref=evidence_ref,
            verification_ref=verification_ref,
            report_ref=state.exports["report.md"],
            sources_ref=state.exports["sources.json"],
        )
        return {**state.exports, "message": self.blob(result)}

    def address(self, run: str) -> CausalEventAddress:
        require(
            run in self.export_addresses and self.state[run].phase == "export_ready",
            "重算发送必须有已保存导出父点",
        )
        # 暂停/恢复可能在明确导出与发送之间新增检查点，父地址仍属于原导出。
        return self.export_addresses[run]

    def sent(self, run: str, data: dict[str, Any]) -> None:
        state = self.state[run]
        identifier = data.get("message_id")
        require(
            identifier in self.messages and identifier not in self.available,
            "协作重算出现未知或重复发送",
        )
        assert isinstance(identifier, str)
        envelope = self.messages[identifier]
        role = state.role.value
        routes = {
            "supervisor": (
                ("researcher", "dispatch", "message:collect"),
                ("verifier", "dispatch", "message:verify"),
                ("writer", "dispatch", "message:write"),
            ),
            "researcher": (("verifier", "evidence", "message"), ("writer", "evidence", "message")),
            "verifier": (("writer", "verification", "message"),),
            "writer": (("supervisor", "result", "message"),),
        }[role]
        index = len(self.pending_sends[run])
        require(index < len(routes), "固定批次发送数量不符")
        target, kind, key = routes[index]
        binding = next(a for a in self.bindings.agents if a.role.value == target)
        expected = CollaborationMessage(
            repository_id=self.index.repository_id,
            collaboration_id=self.index.collaboration_id,
            message_id=str(uuid5(UUID(self.index.collaboration_id), f"{role}:{kind}:{target}")),
            sender=run,
            receiver=binding.agent_run_id,
            task_id=binding.task_id,
            correlation_id=self.index.collaboration_id,
            kind=cast(MessageKind, kind),
            payload_ref=state.exports[key],
            parent_event_id=self.address(run),
        )
        require(expected == envelope, "重算消息与实际来源信封不符")
        authorize_message(self.program, self.bindings, envelope)
        self.available[identifier] = envelope
        self.pending_sends[run].append(identifier)

    def control(self, item: CollaborationSourceEvent, data: dict[str, Any]) -> None:
        run, event = item.run_id, item.event
        if event.type == "collaboration.group_control_requested":
            fact = parse_contract(encode(data), CollaborationControlFact)
            require(
                self.state[run].role.value == "supervisor"
                and fact.collaboration_id == self.index.collaboration_id
                and fact.revision == self.revision + 1
                and fact.actor == self.source.actor
                and set(fact.managed_runs) <= self.roles.keys()
                and fact.action == ("pause" if self.group_action == "resume" else "resume"),
                "重算组控制角色、版本或暂停/恢复次序不符",
            )
            if self.facts:
                require(fact.actor == next(iter(self.facts.values())).actor, "组控制操作者更换")
            self.facts[(run, event.seq)] = fact
            self.revision, self.group_action = fact.revision, fact.action
        else:
            action = data.get("action")
            require(action in ("pause", "resume"), "成功录制出现不支持的角色控制")
            if event.cause is not None:
                cause = event.cause
                origin_fact = self.facts.get(
                    (run if cause["run"] == "self" else str(cause["run"]), int(cause["seq"]))
                )
                require(
                    origin_fact is not None
                    and run in origin_fact.managed_runs
                    and origin_fact.action == action
                    and data.get("collaboration_id") == self.index.collaboration_id
                    and data.get("group_revision") == origin_fact.revision,
                    "角色控制缺少确切组来源",
                )
            else:
                require(data == dict(action=action), "单角色控制混入未声明的输入")
            self.controls[run] = action

    def replay(self) -> CollaborationReplayResult:
        boundaries = {(r.run_id, f.last_seq): f for r in self.index.roles for f in r.frames}
        expected = {
            (r.run_id, e.seq): e
            for r in self.index.roles
            for f in r.frames
            for e in parse_contract(self.read(f.event_manifest_ref), EventManifest).events
        }
        require(
            len(expected) == len(self.source.events)
            and [e.order for e in self.source.events]
            == sorted({e.order for e in self.source.events}),
            "重算事件流不完整或重复",
        )
        for item in self.source.events:
            run, event = item.run_id, item.event
            require(
                (run, event.seq) not in self.seen and expected.get((run, event.seq)) == event,
                "重算事件不是已固定清单",
            )
            if event.cause:
                origin = run if event.cause["run"] == "self" else str(event.cause["run"])
                require((origin, int(event.cause["seq"])) in self.seen, "重算缺少先行因果事件")
            self.seen.add((run, event.seq))
            data = parse_contract(self.read(event.payload_ref), EventPayload).data
            state = self.state[run]
            if event.type in ("control.requested", "collaboration.group_control_requested"):
                self.control(item, data)
            elif event.type == "collaboration.message_sent":
                self.action_allowed(run)
                self.sent(run, data)
            elif event.type == "collaboration.shared_published":
                address = self.address(run)
                head = advance_shared_artifact(
                    self.program,
                    self.bindings,
                    self.shared.get(data["path"]),
                    path=data["path"],
                    expected_version=data["version"] - 1,
                    object_ref=state.exports[data["path"]],
                    publisher_event=address,
                )
                require(head in self.source.publications, "重算共享版本与来源不符")
                self.shared[head.path] = head
                self.pending_heads[run].append(head)
            elif event.type == "collaboration.message_received":
                message = self.available.get(str(data.get("message_id", "")))
                require(
                    message is not None
                    and message.receiver == run
                    and message.message_id not in state.received,
                    "重算收件箱没有已发送的自身消息",
                )
                assert message is not None
                self.state[run] = evolve(state, received=(*state.received, message.message_id))
            elif event.type in ("llm.requested", "tool.started"):
                self.action_allowed(run)
                pending, request = self.call_spec(run)
                require(
                    state.pending == pending and data.get("request_ref") == pending.request_ref,
                    "调用意图没有当前重算预留",
                )
                occurrence = CallOccurrence(
                    logical_step_id=pending.step,
                    call_index=0,
                    attempt=1,
                    provider=pending.provider,
                    signature_digest=pending.signature,
                    request_digest=pending.request_ref,
                )
                require(
                    run not in self.started
                    and data
                    == dict(
                        occurrence=occurrence.model_dump(mode="json"),
                        request_ref=pending.request_ref,
                        effect=pending.effect.value,
                    ),
                    "调用意图 occurrence 不符",
                )
                self.started[run] = event.seq
            elif event.type in ("llm.completed", "tool.completed"):
                pending, request = self.call_spec(run)
                require(
                    state.pending == pending and run not in self.observations,
                    "重算响应没有唯一未决意图",
                )
                occurrence = CallOccurrence(
                    logical_step_id=pending.step,
                    call_index=0,
                    attempt=1,
                    provider=pending.provider,
                    signature_digest=pending.signature,
                    request_digest=pending.request_ref,
                )
                outcome = self.adapters[run].take(
                    occurrence,
                    pending.effect,
                    request,
                    checkpoint=self.checkpoint[run],
                    max_bytes=pending.max_bytes,
                )
                record = self.adapters[run].recordings[self.adapters[run].position - 1]
                result_ref = object_digest(
                    ObjectType.CALL, encode(record.saved.model_dump(mode="json"))
                )
                require(
                    event.cause == dict(run="self", seq=self.started.pop(run, 0))
                    and data
                    == dict(
                        occurrence=occurrence.model_dump(mode="json"),
                        result_ref=result_ref,
                        error=None,
                    ),
                    "重算响应缺少匹配的意图因果或结果对象",
                )
                require(outcome.error is None, "成功协作来源出现调用错误")
                if pending.category == "tool":
                    arguments = decode(request)
                    response = (
                        dict(
                            schema_version=1,
                            source_ids=list(self.corpus.search(tuple(arguments["frameworks"]))),
                        )
                        if pending.name == "corpus_search"
                        else dict(
                            schema_version=1,
                            source=self.corpus.read(arguments["source_id"]).model_dump(mode="json"),
                        )
                        if pending.name == "source_read"
                        else dict(
                            schema_version=1,
                            path=arguments["path"],
                            content_ref=object_digest(ObjectType.BLOB, arguments["text"].encode()),
                        )
                    )
                    require(
                        outcome.payload == encode(response), "重算工具结果不符合固定来源/写入契约"
                    )
                self.observations[run] = outcome
                self.last_response[run] = outcome.payload
            elif (run, event.seq) not in boundaries:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "重算出现未声明的非检查点事件")
            frame = boundaries.get((run, event.seq))
            if frame is not None:
                self.frame(run, event.type, frame)
        require(
            len(self.available) == 7
            and len(self.shared) == 4
            and all(s.status == RunStatus.COMPLETED for s in self.state.values()),
            "协作重算未到完整成功终点",
        )
        for adapter in self.adapters.values():
            adapter.assert_consumed()
        writer = next(r for r, s in self.state.items() if s.role.value == "writer")
        return CollaborationReplayResult(
            tuple(tuple(self.saved[r.run_id]) for r in self.index.roles),
            tuple(tuple(self.refs[r.run_id]) for r in self.index.roles),
            tuple((p, b) for p, (b, _) in self.outputs(writer).items()),
            sum(a.position for a in self.adapters.values()),
            tuple(self.boundaries),
        )

    def frame(self, run: str, event: str, frame: Any) -> None:
        state = self.state[run]
        witness = parse_contract(self.read(frame.state_ref), StateSnapshot)
        if event == "collaboration.resumed":
            self.action_allowed(run)
            state = evolve(state, status=RunStatus.RUNNING, error_code=None)
        elif event == "collaboration.call_reserved":
            self.action_allowed(run)
            require(state.pending is None, "重算不能重复预留")
            pending, request = self.call_spec(run)
            state = evolve(
                state,
                pending=pending,
                usage=evolve(
                    state.usage,
                    steps=state.usage.steps + 1,
                    model_calls=state.usage.model_calls + int(pending.category == "model"),
                    tool_calls=state.usage.tool_calls + int(pending.category == "tool"),
                    reserved_tokens=len(request) + pending.max_bytes
                    if pending.category == "model"
                    else 0,
                ),
            )
        elif event == "collaboration.call_observed":
            require(state.pending is not None and run in self.observations, "重算缺少已保存响应")
            outcome = self.observations.pop(run)
            assert state.pending is not None
            usage = evolve(
                state.usage,
                reserved_tokens=0,
                tokens=state.usage.tokens
                + (
                    len(self.read(state.pending.request_ref)) + len(outcome.payload)
                    if state.pending.category == "model"
                    else 0
                ),
            )
            state = evolve(state, pending=None, usage=usage, traces=(*state.traces, outcome.trace))
        elif event in (
            "collaboration.decision_observed",
            "collaboration.sources_selected",
            "collaboration.source_observed",
        ):
            self.action_allowed(run)
            updated = self.local(run, state)
            require(updated != state, "重算缺少新的领域转换")
            state = updated
        elif event == "collaboration.artifact_observed":
            require(state.role.value == "writer", "其他角色不能生成 Writer 产物")
            path = ("report.md", "sources.json")[len(state.exports)]
            output, _ = self.outputs(run)[path]
            require(
                state.traces[-1].logical_step_id == f"write-{len(state.exports)}",
                "产物缺少真实录制写结果",
            )
            state = evolve(state, exports={**state.exports, path: self.blob(output)})
        elif event == "collaboration.export_ready":
            self.action_allowed(run)
            state = evolve(state, exports=self.export(run), phase="export_ready")
        elif event == "collaboration.role_transition":
            self.action_allowed(run)
            expected = {
                "supervisor": (3, 0),
                "researcher": (2, 1),
                "verifier": (1, 2),
                "writer": (1, 2),
            }[state.role.value]
            require(
                (len(self.pending_sends[run]), len(self.pending_heads[run])) == expected,
                "重算交换批次不完整",
            )
            supervisor = state.role.value == "supervisor"
            state = evolve(
                state,
                sent=(*state.sent, *self.pending_sends[run]),
                publications=(*state.publications, *self.pending_heads[run]),
                phase="await_result" if supervisor else "done",
                status=RunStatus.PAUSED if supervisor else RunStatus.COMPLETED,
            )
            self.pending_sends[run].clear()
            self.pending_heads[run].clear()
        elif event == "collaboration.completed":
            self.action_allowed(run)
            require(
                state.role.value == "supervisor" and len(state.decisions) == 2,
                "协作终点缺少汇总模型决策",
            )
            self.incoming(run, "result")
            state = evolve(state, status=RunStatus.COMPLETED, phase="done")
        elif event == "collaboration.stopped":
            require(
                self.group_action == "pause"
                or self.controls[run] == "pause"
                or not self.ready(run),
                "停止点缺少控制或依赖依据",
            )
            candidates = (state, self.local(run, state))
            valid = {
                encode(s.model_dump(mode="json")): s
                for c in candidates
                for s in (evolve(c, status=RunStatus.PAUSED, error_code=None),)
                if s.model_dump(mode="json") == witness.data
            }
            require(len(valid) == 1, "停止快照不是允许的调用后局部转换")
            state = next(iter(valid.values()))
            self.boundaries.append(
                (
                    frame.commit_id,
                    "after_local"
                    if state.phase != self.state[run].phase
                    or state.decisions != self.state[run].decisions
                    or state.evidence != self.state[run].evidence
                    or state.source_ids != self.state[run].source_ids
                    else "before_local",
                )
            )
        else:
            require(event == "collaboration.inbox_observed", "未声明的协作检查点类型")
        limits = self.program.policy(state.role).limits
        require(
            state.usage.steps <= limits.max_steps
            and state.usage.model_calls <= limits.max_model_calls
            and state.usage.tool_calls <= limits.max_tool_calls
            and state.usage.tokens + state.usage.reserved_tokens <= limits.max_tokens,
            "重算状态超过独立角色预算",
        )
        artifacts = tuple(
            ArtifactEntry(
                path=p,
                object_ref=ref,
                media_type="text/markdown" if p.endswith(".md") else "application/json",
            )
            for p, ref in sorted(state.exports.items())
            if not p.startswith("message")
        )
        snapshot = StateSnapshot(data=state.model_dump(mode="json"), artifacts=artifacts)
        ref = object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
        require(snapshot == witness and ref == frame.state_ref, "重算状态或产物与来源检查点不符")
        self.state[run], self.checkpoint[run] = state, frame.commit_id
        if event == "collaboration.export_ready":
            source_event = parse_contract(
                self.read(frame.event_manifest_ref), EventManifest
            ).events[-1]
            self.export_addresses[run] = CausalEventAddress(
                agent_run_id=run,
                commit_id=frame.commit_id,
                seq=source_event.seq,
                payload_ref=source_event.payload_ref,
            )
        self.saved[run].append(state)
        self.refs[run].append(ref)
