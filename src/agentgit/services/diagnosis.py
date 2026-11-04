"""当前项目的只读行为证据；不启动 Adapter、Job、容器或改变历史。"""

from collections import defaultdict
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.diagnosis import (
    MAX_TRACE_EDGES,
    MAX_TRACE_EVENTS,
    DependencyEdge,
    EventReference,
    EvidenceGap,
    TraceGraph,
    TraceNode,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS
from agentgit.core.models import CallIntent, CallOccurrence, CommitManifest, parse_contract
from agentgit.core.research import AgentState, ModelRequest, SavedCallOutcome
from agentgit.core.snapshots import EventManifest, EventPayload, LogicalEvent, StateSnapshot
from agentgit.diagnosis.graph import contextual_edges
from agentgit.diff.behavioral import behavioral_diff
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.versions import VersionService

MAX_TRACE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
CALL_EVENTS = {
    "llm.requested",
    "llm.completed",
    "tool.started",
    "tool.completed",
    "call.failed",
    "call.outcome_unknown",
    "call.reused",
}


class BehaviorService:
    def __init__(self, history: HistoryService):
        self.history = history
        self.versions = VersionService(history)

    def trace(self, repository: UUID, run_id: UUID, *, anchor: str | None = None) -> TraceGraph:
        events, links, snapshots, commits, calls, refs = (
            TABLES[name]
            for name in (
                "events",
                "run_commits",
                "snapshots",
                "commits",
                "external_calls",
                "object_refs",
            )
        )
        with self.history.engine.connect() as connection:
            run = dict(self.history._run(connection, repository, run_id))
            cutoff = int(run["last_event_seq"])
            if cutoff > MAX_TRACE_EVENTS:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "行为轨迹超过事件上限，不能只比较前缀")
            rows = [
                dict(row)
                for row in connection.execute(
                    sa.select(events)
                    .where(
                        events.c.repository_id == repository,
                        events.c.run_id == run_id,
                        events.c.seq <= cutoff,
                    )
                    .order_by(events.c.seq)
                ).mappings()
            ]
            checkpoints = [
                dict(row)
                for row in connection.execute(
                    sa.select(links.c.run_order, snapshots.c.event_seq, commits)
                    .join(
                        snapshots,
                        sa.and_(
                            snapshots.c.repository_id == links.c.repository_id,
                            snapshots.c.run_id == links.c.run_id,
                            snapshots.c.commit_id == links.c.commit_id,
                        ),
                    )
                    .join(
                        commits,
                        sa.and_(
                            commits.c.repository_id == links.c.repository_id,
                            commits.c.id == links.c.commit_id,
                        ),
                    )
                    .where(
                        links.c.repository_id == repository,
                        links.c.run_id == run_id,
                        snapshots.c.event_seq <= cutoff,
                    )
                    .order_by(links.c.run_order)
                    .limit(MAX_TRACE_EVENTS + 1)
                ).mappings()
            ]
            call_rows = [
                dict(row)
                for row in connection.execute(
                    sa.select(calls)
                    .where(calls.c.repository_id == repository, calls.c.run_id == run_id)
                    .limit(MAX_TRACE_EVENTS + 1)
                ).mappings()
            ]
            owner_ids = (
                [f"{run_id}:{row['seq']}" for row in rows]
                + [str(row["id"]) for row in call_rows]
                + [str(row["id"]) for row in checkpoints]
            )
            ownership = {
                tuple(row)
                for row in connection.execute(
                    sa.select(refs.c.owner_type, refs.c.owner_id, refs.c.role, refs.c.object_id)
                    .where(
                        refs.c.repository_id == repository,
                        refs.c.owner_id.in_(owner_ids),
                        refs.c.role.in_(
                            (
                                "payload",
                                "intent",
                                "request",
                                "response",
                                "result",
                                "uncertain_diagnostic",
                                "manifest",
                                "state",
                                "events",
                            )
                        ),
                    )
                    .limit(MAX_TRACE_EVENTS * 12 + 1)
                )
            }
        if len(rows) != cutoff or any(row["seq"] != index for index, row in enumerate(rows, 1)):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为事件索引不连续")
        if (
            len(call_rows) > MAX_TRACE_EVENTS
            or len(checkpoints) > MAX_TRACE_EVENTS
            or len(ownership) > MAX_TRACE_EVENTS * 12
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "行为证据索引超过上限")
        if any(row["run_order"] != index for index, row in enumerate(checkpoints, 1)) or (
            checkpoints and checkpoints[-1]["id"] != run["checkpoint_commit_id"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为检查点索引与 Run HEAD 不符")
        head = run["checkpoint_commit_id"]
        lineage = self.versions.ancestors(repository, head) if head else []
        base = run["base_commit_id"]
        if base is not None and base not in lineage:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为 Run 的来源起点不在版本路径中")
        own_path = list(reversed(lineage[: lineage.index(base)] if base is not None else lineage))
        if own_path != [row["id"] for row in checkpoints]:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为 Run 的检查点路径存在缺口")
        if anchor is not None and anchor not in lineage:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "行为比较祖先不属于该 Run")
        anchor = anchor if anchor is not None else lineage[-1] if lineage else None
        cache: dict[tuple[ObjectType, str], bytes] = {}
        total = 0

        def owned(owner_type: str, owner: str, role: str, ref: str, kind: ObjectType) -> bytes:
            nonlocal total
            if (owner_type, owner, role, ref) not in ownership:
                raise AgentGitError(ErrorCode.NOT_FOUND, "行为证据引用归属不可访问")
            key = kind, ref
            if key not in cache:
                payload = self.history.objects.get(ref, kind)
                total += len(payload)
                if total > MAX_TRACE_BYTES:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "行为内容超过读取字节上限")
                cache[key] = payload
            return cache[key]

        expected: dict[int, LogicalEvent] = {}
        states: dict[str, AgentState] = {}
        state_events: dict[str, str] = {}
        for row in checkpoints:
            identity = str(row["id"])
            manifest = parse_contract(
                owned("commit", identity, "manifest", identity, ObjectType.COMMIT), CommitManifest
            )
            if (
                manifest.repository_id != str(repository)
                or any(
                    getattr(manifest, key) != row[key]
                    for key in ("state_ref", "environment_ref", "event_manifest_ref")
                )
                or manifest.event_range.last_seq != row["event_seq"]
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为检查点清单与索引不符")
            event_manifest = parse_contract(
                owned(
                    "commit",
                    identity,
                    "events",
                    manifest.event_manifest_ref,
                    ObjectType.EVENT_MANIFEST,
                ),
                EventManifest,
            )
            if [item.seq for item in event_manifest.events] != list(
                range(manifest.event_range.first_seq, manifest.event_range.last_seq + 1)
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为事件清单范围不完整")
            for item in event_manifest.events:
                if item.seq in expected:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为检查点事件范围重叠")
                expected[item.seq] = item
            snapshot = parse_contract(
                owned("commit", identity, "state", manifest.state_ref, ObjectType.STATE),
                StateSnapshot,
            )
            if "goal" in snapshot.data and "plan_version" in snapshot.data:
                states[identity] = parse_contract(encode(snapshot.data), AgentState)
                state_events[identity] = f"{run_id}:{row['event_seq']}"
        call_index = {
            (row["logical_step_id"], row["call_index"], row["attempt"]): row for row in call_rows
        }
        committed_seq = checkpoints[-1]["event_seq"] if checkpoints else 0
        if set(expected) != set(range(1, committed_seq + 1)):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为已提交事件范围存在缺口")
        nodes: list[TraceNode] = []
        gaps: list[EvidenceGap] = []
        call_checkpoints: dict[str, str] = {}
        verified_model_contexts: set[str] = set()
        for row in rows:
            reference = EventReference(run_id=str(run_id), seq=row["seq"], event_id=row["id"])
            logical = LogicalEvent(
                seq=row["seq"],
                type=row["type"],
                logical_step_id=row["logical_step_id"],
                payload_ref=row["payload_ref"],
                cause=self.history._logical_cause(
                    row["caused_by_run_id"] or run_id, row["caused_by_seq"], run_id
                ),
            )
            if row["seq"] in expected and expected[row["seq"]] != logical:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为事件索引与已提交清单不同")
            if row["seq"] not in expected:
                gaps.append(EvidenceGap(event=reference, reason="uncommitted_event"))
            data: dict[str, Any] = {}
            verified = True
            try:
                data = parse_contract(
                    owned("event", reference.key, "payload", row["payload_ref"], ObjectType.EVENT),
                    EventPayload,
                ).data
            except AgentGitError as error:
                if (
                    error.code != ErrorCode.NOT_FOUND
                    or ("event", reference.key, "payload", row["payload_ref"]) not in ownership
                ):
                    raise
                gaps.append(EvidenceGap(event=reference, reason="missing_payload"))
                verified = False
            occurrence = None
            observation = None
            error_code = (
                data.get("error", {}).get("code") if isinstance(data.get("error"), dict) else None
            )
            phase = str(row["type"])
            if row["type"] in CALL_EVENTS and "occurrence" not in data and verified:
                gaps.append(EvidenceGap(event=reference, reason="unverified_occurrence"))
                verified = False
            if row["type"] in CALL_EVENTS and "occurrence" in data:
                occurrence = parse_contract(encode(data["occurrence"]), CallOccurrence)
                call = call_index.get(
                    (occurrence.logical_step_id, occurrence.call_index, occurrence.attempt)
                )
                if call is None or any(
                    call[key] != getattr(occurrence, key)
                    for key in (
                        "logical_step_id",
                        "provider",
                        "call_index",
                        "attempt",
                        "signature_digest",
                        "request_digest",
                    )
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为事件与调用索引不符")
                owner = str(call["id"])
                intent = parse_contract(
                    owned("external_call", owner, "intent", call["intent_ref"], ObjectType.CALL),
                    CallIntent,
                )
                if (
                    intent.occurrence != occurrence
                    or intent.request_ref != occurrence.request_digest
                    or intent.effect.value != call["effect"]
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为调用意图与 occurrence 不符")
                request_bytes = owned(
                    "external_call", owner, "request", intent.request_ref, ObjectType.BLOB
                )
                if intent.checkpoint is not None:
                    if intent.checkpoint not in lineage:
                        raise AgentGitError(
                            ErrorCode.OBJECT_CORRUPT, "行为调用检查点不属于当前版本路径"
                        )
                    call_checkpoints[reference.key] = intent.checkpoint
                phase = "call.result" if "result_ref" in data else "call.request"
                if (phase == "call.request") != (row["type"] in {"llm.requested", "tool.started"}):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为调用事件阶段与类型不符")
                if phase == "call.request" and occurrence.provider.startswith("llm:"):
                    try:
                        request = parse_contract(request_bytes, ModelRequest)
                    except AgentGitError as error:
                        if error.code not in (ErrorCode.VALIDATION, ErrorCode.UNSUPPORTED_SCHEMA):
                            raise
                    else:
                        if request.state == states.get(intent.checkpoint or ""):
                            verified_model_contexts.add(reference.key)
                if "result_ref" in data:
                    result_ref = data["result_ref"]
                    role = (
                        "uncertain_diagnostic"
                        if row["type"] == "call.outcome_unknown"
                        else "result"
                    )
                    if role == "result" and result_ref != call["result_ref"]:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为事件结果与调用索引不同")
                    saved = parse_contract(
                        owned("external_call", owner, role, result_ref, ObjectType.CALL),
                        SavedCallOutcome,
                    )
                    if "error" in data and data["error"] != saved.error:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为事件与录制错误不同")
                    if role == "uncertain_diagnostic" and (
                        saved.error is None or saved.error["code"] != "outcome_unknown"
                    ):
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "未知结果事件缺少对应诊断")
                    error_code = saved.error["code"] if saved.error else None
                    try:
                        response_bytes = owned(
                            "external_call", owner, "response", saved.response_ref, ObjectType.BLOB
                        )
                        if (
                            len(response_bytes) > intent.max_response_bytes
                            or saved.response_bytes < len(response_bytes)
                            or (saved.complete and saved.response_bytes != len(response_bytes))
                        ):
                            raise AgentGitError(
                                ErrorCode.OBJECT_CORRUPT, "行为观测长度与冻结录制不同"
                            )
                        observation = saved.response_ref
                    except AgentGitError as error:
                        if (
                            error.code != ErrorCode.NOT_FOUND
                            or ("external_call", owner, "response", saved.response_ref)
                            not in ownership
                        ):
                            raise
                        gaps.append(EvidenceGap(event=reference, reason="missing_observation"))
                        verified = False
                    if not saved.complete:
                        gaps.append(EvidenceGap(event=reference, reason="incomplete_observation"))
                    if saved.error and saved.error["code"] == "outcome_unknown":
                        gaps.append(EvidenceGap(event=reference, reason="uncertain_outcome"))
                elif data.get("request_ref") != intent.request_ref:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为开始事件的规范请求不同")
            nodes.append(
                TraceNode(
                    event=reference,
                    event_type=row["type"],
                    logical_step_id=row["logical_step_id"],
                    phase=phase,
                    payload_ref=row["payload_ref"],
                    occurrence=occurrence,
                    observation_ref=observation,
                    error_code=error_code,
                    payload_verified=verified,
                )
            )
        by_seq = {node.event.seq: node for node in nodes}
        edges: list[DependencyEdge] = []
        requests: dict[tuple[str, str, int, int], list[TraceNode]] = defaultdict(list)
        for node in nodes:
            if node.phase == "call.request" and node.payload_verified:
                requests[node.identity[1:]].append(node)
        for node in nodes:
            if node.phase != "call.result" or not node.payload_verified:
                continue
            request_nodes = requests.get(node.identity[1:], [])
            request_node = request_nodes[0] if len(request_nodes) == 1 else None
            if request_node is not None and request_node.occurrence == node.occurrence:
                edges.append(
                    DependencyEdge(
                        source=request_node.event, target=node.event, basis="call_occurrence"
                    )
                )
            elif request_nodes or node.event_type != "call.reused":
                gaps.append(EvidenceGap(event=node.event, reason="unverified_occurrence"))
        with self.history.engine.connect() as connection:
            for row in rows:
                seq = row["caused_by_seq"]
                if seq is None:
                    continue
                node = by_seq[row["seq"]]
                source_run = row["caused_by_run_id"] or run_id
                self.history._run(connection, repository, source_run)
                if source_run == run_id and seq in by_seq:
                    if seq >= row["seq"]:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "行为因果引用指向未来")
                    edges.append(
                        DependencyEdge(
                            source=by_seq[seq].event, target=node.event, basis="caused_by"
                        )
                    )
                else:
                    source = connection.execute(
                        sa.select(events.c.id).where(
                            events.c.repository_id == repository,
                            events.c.run_id == source_run,
                            events.c.seq == seq,
                        )
                    ).scalar_one_or_none()
                    related = EventReference(run_id=str(source_run), seq=seq, event_id=source)
                    gaps.append(
                        EvidenceGap(
                            event=node.event,
                            related=related,
                            reason="outside_trace" if source else "missing_cause",
                        )
                    )
        contextual, context_gaps = contextual_edges(
            tuple(nodes), states, state_events, call_checkpoints, verified_model_contexts
        )
        edges.extend(contextual)
        gaps.extend(context_gaps)
        unique_edges = {encode(edge.model_dump(mode="json")): edge for edge in edges}
        unique_gaps = {encode(gap.model_dump(mode="json")): gap for gap in gaps}
        if len(unique_edges) > MAX_TRACE_EDGES or len(unique_gaps) > MAX_TRACE_EDGES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "行为依赖边或证据缺口超过上限")
        graph = TraceGraph(
            repository_id=str(repository),
            run_id=str(run_id),
            anchor_commit=anchor,
            head_commit=head,
            run_status=run["status"],
            scope_complete=run["status"] in TERMINAL_RUNS and bool(head),
            as_of_seq=cutoff,
            nodes=tuple(nodes),
            edges=tuple(unique_edges[key] for key in sorted(unique_edges)),
            gaps=tuple(unique_gaps[key] for key in sorted(unique_gaps)),
        )
        if len(encode(graph.model_dump(mode="json"))) > MAX_REPORT_BYTES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "行为轨迹超过输出字节上限")
        return graph

    def compare(self, repository: UUID, left: UUID, right: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            a = self.history._run(connection, repository, left)
            b = self.history._run(connection, repository, right)
        left_path = (
            self.versions.ancestors(repository, a["checkpoint_commit_id"])
            if a["checkpoint_commit_id"]
            else []
        )
        right_path = (
            self.versions.ancestors(repository, b["checkpoint_commit_id"])
            if b["checkpoint_commit_id"]
            else []
        )
        right_ancestors = set(right_path)
        common = next((item for item in left_path if item in right_ancestors), None)
        x, y = (
            self.trace(repository, left, anchor=common),
            self.trace(repository, right, anchor=common),
        )
        if common is None:
            x = TraceGraph.model_validate({**x.model_dump(), "anchor_commit": None})
            y = TraceGraph.model_validate({**y.model_dump(), "anchor_commit": None})
        result = behavioral_diff(x, y)
        response = {
            "schema_version": 1,
            "left": x.model_dump(mode="json"),
            "right": y.model_dump(mode="json"),
            "comparison": result.model_dump(mode="json"),
        }
        if len(encode(response)) > MAX_REPORT_BYTES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "行为报告超过输出字节上限")
        return response
