"""确定的依赖感知匹配；重复身份没有唯一证据时保留歧义。"""

from collections import defaultdict
from heapq import heapify, heappop, heappush

from agentgit.core.diagnosis import (
    AlignmentAmbiguity,
    BehavioralDiff,
    Difference,
    EventMatch,
    EventReference,
    MatchBasis,
    TraceGraph,
    TraceNode,
)
from agentgit.core.errors import AgentGitError, ErrorCode

MAX_COMPARISONS = 200000


def _parents(graph: TraceGraph) -> dict[str, set[tuple[str, str]]]:
    result = {node.event.key: set[tuple[str, str]]() for node in graph.nodes}
    for edge in graph.edges:
        result[edge.target.key].add((edge.source.key, edge.basis))
    return result


def _content(node: TraceNode) -> tuple[str, str | None, str | None, str | None]:
    call = node.occurrence
    return (
        node.observation_ref or node.payload_ref,
        call.signature_digest if call else None,
        call.request_digest if call else None,
        node.error_code,
    )


def _frontier(graph: TraceGraph, changed: set[str]) -> tuple[EventReference, ...]:
    nodes = {node.event.key: node for node in graph.nodes}
    parents = {key: set[str]() for key in nodes}
    children = {key: set[str]() for key in nodes}
    for edge in graph.edges:
        parents[edge.target.key].add(edge.source.key)
        children[edge.source.key].add(edge.target.key)
    remaining = {key: len(incoming) for key, incoming in parents.items()}
    ready = [key for key, count in remaining.items() if not count]
    heapify(ready)
    prior = {key: False for key in nodes}
    frontier = []
    while ready:
        key = heappop(ready)
        if key in changed and not prior[key]:
            frontier.append(nodes[key].event)
        for target in children[key]:
            prior[target] = prior[target] or prior[key] or key in changed
            remaining[target] -= 1
            if not remaining[target]:
                heappush(ready, target)
    return tuple(sorted(frontier, key=lambda ref: ref.key))


def behavioral_diff(left: TraceGraph, right: TraceGraph) -> BehavioralDiff:
    if left.repository_id != right.repository_id:
        raise AgentGitError(ErrorCode.NOT_FOUND, "比较轨迹不属于同一项目")
    if left.anchor_commit is None or left.anchor_commit != right.anchor_commit:
        return BehavioralDiff(
            status="no_shared_ancestor", common_ancestor=None, complete_evidence=False
        )
    left_nodes = {node.event.key: node for node in left.nodes}
    right_nodes = {node.event.key: node for node in right.nodes}
    left_parents, right_parents = _parents(left), _parents(right)
    left_groups: dict[tuple[str, str, str, int, int], set[str]] = defaultdict(set)
    right_groups: dict[tuple[str, str, str, int, int], set[str]] = defaultdict(set)
    for node in left.nodes:
        left_groups[node.identity].add(node.event.key)
    for node in right.nodes:
        right_groups[node.identity].add(node.event.key)
    mapping: dict[str, str] = {}
    reverse_mapping: dict[str, str] = {}
    bases: dict[str, MatchBasis] = {}
    groups = sorted(left_groups.keys() | right_groups.keys())

    def accept(source: str, target: str, basis: MatchBasis) -> None:
        mapping[source], bases[source] = target, basis
        reverse_mapping[target] = source
        left_groups[left_nodes[source].identity].remove(source)
        right_groups[right_nodes[target].identity].remove(target)

    for identity in groups:
        a, b = left_groups[identity], right_groups[identity]
        if len(a) == len(b) == 1:
            accept(next(iter(a)), next(iter(b)), "logical_identity")
    comparisons = 0
    while True:
        proposed: list[tuple[str, str, MatchBasis]] = []
        for identity in groups:
            a, b = left_groups[identity], right_groups[identity]
            if not a or not b:
                continue
            comparisons += len(a) * len(b)
            if comparisons > MAX_COMPARISONS:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "重复事件对齐超过有界比较上限")
            candidates: dict[str, list[tuple[str, MatchBasis]]] = defaultdict(list)
            reverse: dict[str, list[str]] = defaultdict(list)
            for source in sorted(a):
                for target in sorted(b):
                    x, y = left_nodes[source], right_nodes[target]
                    if not x.payload_verified or not y.payload_verified:
                        continue
                    incoming, outgoing = left_parents[source], right_parents[target]
                    if any(parent not in mapping for parent, _ in incoming) or any(
                        parent not in reverse_mapping for parent, _ in outgoing
                    ):
                        continue
                    translated = {(mapping[parent], kind) for parent, kind in incoming}
                    if translated != outgoing:
                        continue
                    basis: MatchBasis
                    if incoming:
                        basis = "dependency_context"
                    elif x.event_type == y.event_type and _content(x) == _content(y):
                        basis = "canonical_content"
                    else:
                        continue
                    candidates[source].append((target, basis))
                    reverse[target].append(source)
            for source, options in candidates.items():
                if len(options) == 1 and len(reverse[options[0][0]]) == 1:
                    target, basis = options[0]
                    proposed.append((source, target, basis))
        if not proposed:
            break
        for source, target, basis in sorted(proposed):
            accept(source, target, basis)
    matches = []
    for source, target in sorted(mapping.items()):
        x, y = left_nodes[source], right_nodes[target]
        changes: list[Difference] = []
        if x.payload_verified and y.payload_verified:
            if x.event_type != y.event_type:
                changes.append("event_type")
            if x.occurrence is not None and y.occurrence is not None:
                if x.occurrence.signature_digest != y.occurrence.signature_digest:
                    changes.append("signature")
                if x.occurrence.request_digest != y.occurrence.request_digest:
                    changes.append("request")
            if x.observation_ref != y.observation_ref:
                changes.append("observation")
            if x.occurrence is None and x.payload_ref != y.payload_ref:
                changes.append("event_payload")
            if x.error_code != y.error_code:
                changes.append("error")
            expected = {
                (mapping.get(parent, "unmatched:" + parent), kind)
                for parent, kind in left_parents[source]
            }
            if expected != right_parents[target]:
                changes.append("dependencies")
        matches.append(
            EventMatch(
                left=x.event,
                right=y.event,
                basis=bases[source],
                comparison="unverified"
                if not (x.payload_verified and y.payload_verified)
                else "changed"
                if changes
                else "equal",
                differences=tuple(changes),
            )
        )
    ambiguities: list[AlignmentAmbiguity] = []
    only_left: list[EventReference] = []
    only_right: list[EventReference] = []
    for identity in groups:
        a, b = left_groups[identity], right_groups[identity]
        if a and b:
            ambiguities.append(
                AlignmentAmbiguity(
                    left=tuple(left_nodes[key].event for key in sorted(a)),
                    right=tuple(right_nodes[key].event for key in sorted(b)),
                )
            )
        else:
            only_left.extend(left_nodes[key].event for key in sorted(a))
            only_right.extend(right_nodes[key].event for key in sorted(b))
    changed_left = {item.left.key for item in matches if item.comparison == "changed"}
    changed_right = {item.right.key for item in matches if item.comparison == "changed"}
    changed_left.update(ref.key for ref in only_left)
    changed_right.update(ref.key for ref in only_right)
    return BehavioralDiff(
        status="comparable",
        common_ancestor=left.anchor_commit,
        matches=tuple(matches),
        left_only=tuple(only_left),
        right_only=tuple(only_right),
        ambiguities=tuple(ambiguities),
        first_left=_frontier(left, changed_left),
        first_right=_frontier(right, changed_right),
        complete_evidence=left.scope_complete
        and right.scope_complete
        and not (
            left.gaps
            or right.gaps
            or ambiguities
            or any(not node.payload_verified for node in (*left.nodes, *right.nodes))
        ),
    )
