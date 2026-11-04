"""有界、可寻址的行为证据；事件顺序不等同于因果依赖。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import CallOccurrence, Contract, Digest, Identifier, NonNegative

MAX_TRACE_EVENTS = 10000
MAX_TRACE_EDGES = 30000
Positive = Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
Text = Annotated[str, Field(min_length=1, max_length=200)]


class EventReference(Contract):
    run_id: Identifier
    seq: Positive
    event_id: Positive | None = None

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.seq}"


class TraceNode(Contract):
    event: EventReference
    event_type: Text
    logical_step_id: Text
    phase: Text
    payload_ref: Digest
    occurrence: CallOccurrence | None = None
    observation_ref: Digest | None = None
    error_code: Text | None = None
    payload_verified: bool = True

    @model_validator(mode="after")
    def same_logical_step(self) -> "TraceNode":
        if self.occurrence is not None and self.occurrence.logical_step_id != self.logical_step_id:
            raise ValueError("事件与调用的 logical step 不一致")
        return self

    @property
    def identity(self) -> tuple[str, str, str, int, int]:
        call = self.occurrence
        return (
            self.phase,
            self.logical_step_id,
            call.provider if call else "",
            call.call_index if call else 0,
            call.attempt if call else 0,
        )


class DependencyEdge(Contract):
    source: EventReference
    target: EventReference
    basis: Literal["caused_by", "call_occurrence", "plan_dependency", "decision_context"]


class EvidenceGap(Contract):
    event: EventReference
    related: EventReference | None = None
    reason: Literal[
        "missing_payload",
        "missing_observation",
        "outside_trace",
        "missing_cause",
        "uncommitted_event",
        "unverified_occurrence",
        "missing_checkpoint",
        "incomplete_observation",
        "uncertain_outcome",
        "unverified_context",
    ]


class TraceGraph(Contract):
    repository_id: Identifier
    run_id: Identifier
    anchor_commit: Digest | None
    head_commit: Digest | None = None
    run_status: Text = "unknown"
    scope_complete: bool = True
    as_of_seq: NonNegative
    nodes: Annotated[tuple[TraceNode, ...], Field(max_length=MAX_TRACE_EVENTS)]
    edges: Annotated[tuple[DependencyEdge, ...], Field(max_length=MAX_TRACE_EDGES)] = ()
    gaps: Annotated[tuple[EvidenceGap, ...], Field(max_length=MAX_TRACE_EDGES)] = ()

    @model_validator(mode="after")
    def valid_graph(self) -> "TraceGraph":
        nodes = {node.event.key: node.event for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("依赖图事件引用重复")
        if any(
            node.event.run_id != self.run_id or node.event.seq > self.as_of_seq
            for node in self.nodes
        ):
            raise ValueError("事件不属于当前轨迹范围")
        edges = {(edge.source.key, edge.target.key, edge.basis) for edge in self.edges}
        if len(edges) != len(self.edges):
            raise ValueError("依赖边重复")
        for edge in self.edges:
            if (
                nodes.get(edge.source.key) != edge.source
                or nodes.get(edge.target.key) != edge.target
            ):
                raise ValueError("依赖边必须引用图内精确事件")
            if edge.source.key == edge.target.key:
                raise ValueError("依赖边不能指向自身")
        # 同一端点的不同依据仍只算一条拓扑边；不递归遍历长链。
        parents = {key: set[str]() for key in nodes}
        children = {key: set[str]() for key in nodes}
        for source, target, _ in edges:
            parents[target].add(source)
            children[source].add(target)
        ready = [key for key, incoming in parents.items() if not incoming]
        consumed = 0
        while ready:
            source = ready.pop()
            consumed += 1
            for target in children[source]:
                parents[target].remove(source)
                if not parents[target]:
                    ready.append(target)
        if consumed != len(nodes):
            raise ValueError("依赖图包含循环")
        if any(nodes.get(gap.event.key) != gap.event for gap in self.gaps):
            raise ValueError("证据缺口必须绑定图内事件")
        return self


Difference = Literal[
    "event_type", "signature", "request", "observation", "event_payload", "error", "dependencies"
]
MatchBasis = Literal["logical_identity", "canonical_content", "dependency_context"]


class EventMatch(Contract):
    left: EventReference
    right: EventReference
    basis: MatchBasis
    comparison: Literal["equal", "changed", "unverified"]
    differences: tuple[Difference, ...] = ()


class AlignmentAmbiguity(Contract):
    left: tuple[EventReference, ...]
    right: tuple[EventReference, ...]
    reason: Literal["repeated_identity"] = "repeated_identity"


class BehavioralDiff(Contract):
    kind: Literal["behavioral_diff/v1"] = "behavioral_diff/v1"
    status: Literal["comparable", "no_shared_ancestor"]
    common_ancestor: Digest | None
    matches: tuple[EventMatch, ...] = ()
    left_only: tuple[EventReference, ...] = ()
    right_only: tuple[EventReference, ...] = ()
    ambiguities: tuple[AlignmentAmbiguity, ...] = ()
    first_left: tuple[EventReference, ...] = ()
    first_right: tuple[EventReference, ...] = ()
    complete_evidence: bool
    limitations: tuple[str, ...] = (
        "对齐按共同祖先与逻辑/调用身份进行，不使用时间戳或事件 ID 消歧。",
        "首个可见分歧是依赖图上的最小前沿，可以有多个；分歧不等同于故障根因。",
        "依赖边保留原始依据；缺失证据和重复身份保留不确定性。",
        "可见分歧前沿仅针对已核验边，存在证据缺口时不保证完整。",
        "当前仅比较已观察轨迹，没有运行干预试验或证明因果改善。",
    )
