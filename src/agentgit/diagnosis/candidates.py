"""失败路径优先的可解释静态候选；不用时间、数据库 ID 或 gold 排名。"""

from collections import defaultdict, deque

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.debug_reports import (
    MAX_ASSERTIONS,
    CandidateFeatures,
    CandidateKind,
    FailureAssertion,
    FaultCandidate,
    StaticDiagnosis,
)
from agentgit.core.diagnosis import BehavioralDiff, EventReference, TraceGraph, TraceNode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import evolve

RULES = {
    "schema_version": 1,
    "version": "static-failure-path/v1",
    "ordering": [
        "failure_path",
        "report_role_priority",
        "failure_distance",
        "divergence_distance",
        "uncertainty_count",
    ],
    "missing_artifacts": ["accepted_plan", "artifact_write", "other"],
    "other_report_assertions": ["artifact_write", "accepted_plan", "other"],
    "runtime_stop": "no_role_preference",
    "divergence_distance": "undirected_distance_to_verified_first_left_frontier",
    "ties": "competition_rank_without_arrival_or_event_id_tiebreak",
    "terminal_output": "exclude_output_only_connected_to_final_evaluation",
    "effect": "unknown_without_intervention",
}
RULES_DIGEST = object_digest(ObjectType.BLOB, encode(RULES))


def _distances(starts: set[str], neighbors: dict[str, set[str]]) -> dict[str, int]:
    result = {key: 0 for key in starts}
    pending = deque(sorted(starts))
    while pending:
        source = pending.popleft()
        for target in neighbors[source]:
            if target not in result:
                result[target] = result[source] + 1
                pending.append(target)
    return result


def static_diagnosis(
    graph: TraceGraph,
    assertions: tuple[FailureAssertion, ...],
    comparison: BehavioralDiff | None = None,
) -> StaticDiagnosis:
    if len(assertions) > MAX_ASSERTIONS:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "失败硬断言超过定位上限")
    nodes = {node.event.key: node for node in graph.nodes}
    for assertion in assertions:
        node = nodes.get(assertion.event.key)
        if node is None or node.event != assertion.event or not node.payload_verified:
            raise AgentGitError(ErrorCode.NOT_FOUND, "失败断言必须引用当前轨迹中已核验的精确事件")
    if len({(item.event.key, item.origin, item.code) for item in assertions}) != len(assertions):
        raise AgentGitError(ErrorCode.VALIDATION, "失败断言不能重复")
    if comparison is not None:
        left_refs = [item.left for item in comparison.matches]
        left_refs.extend(comparison.left_only)
        left_refs.extend(comparison.first_left)
        left_refs.extend(ref for item in comparison.ambiguities for ref in item.left)
        if any(ref.key not in nodes or nodes[ref.key].event != ref for ref in left_refs):
            raise AgentGitError(ErrorCode.NOT_FOUND, "参考比较必须绑定当前失败侧精确事件")
        if comparison.status == "comparable" and (
            comparison.common_ancestor is None or comparison.common_ancestor != graph.anchor_commit
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "参考比较祖先与失败轨迹不一致")
    parents = {key: set[str]() for key in nodes}
    neighbors = {key: set[str]() for key in nodes}
    context_children = {key: set[str]() for key in nodes}
    for edge in graph.edges:
        a, b = edge.source.key, edge.target.key
        parents[b].add(a)
        neighbors[a].add(b)
        neighbors[b].add(a)
        if edge.basis == "decision_context":
            context_children[a].add(b)
    failures = [_distances({item.event.key}, parents) for item in assertions]
    frontier: set[str] = set()
    if comparison is not None and comparison.status == "comparable":
        for reference in comparison.first_left:
            node = nodes.get(reference.key)
            if node is None or node.event != reference:
                raise AgentGitError(ErrorCode.NOT_FOUND, "参考对齐的失败侧事件不属于当前轨迹")
            if node.payload_verified:
                frontier.add(reference.key)
    divergence = _distances(frontier, neighbors)
    gap_events = {gap.event.key for gap in graph.gaps}
    ambiguous = (
        {ref.key for item in comparison.ambiguities for ref in item.left}
        if comparison
        else set[str]()
    )
    report_errors = [item.code for item in assertions if item.origin == "research_validator"]
    candidates: list[FaultCandidate] = []
    excluded: list[EventReference] = []

    def verified(key: str) -> bool:
        return nodes[key].payload_verified and key not in gap_events

    for node in graph.nodes:
        if (
            node.phase != "call.result"
            or node.occurrence is None
            or not node.occurrence.provider.startswith("llm:")
        ):
            continue
        key = node.event.key
        accepted = [
            nodes[target]
            for target in context_children[key]
            if verified(target)
            and nodes[target].event_type in {"plan.accepted", "decision.accepted"}
        ]
        terminal = [
            nodes[target]
            for target in context_children[key]
            if verified(target) and nodes[target].event_type == "run.evaluated"
        ]
        if terminal and not accepted and verified(key):
            excluded.append(node.event)
            continue
        kind: CandidateKind
        kind = "unaccepted_model_output"
        action: TraceNode | None = None
        support = {key}
        uncertainties: list[str] = []
        if not verified(key):
            uncertainties.append("模型观测或相关依赖缺少完整证据")
        elif len(accepted) == 1:
            state_event = accepted[0]
            support.add(state_event.event.key)
            if state_event.event_type == "plan.accepted":
                kind = "accepted_plan"
            else:
                targets = []
                for target in context_children[state_event.event.key]:
                    item = nodes[target]
                    if (
                        verified(target)
                        and item.phase == "call.request"
                        and item.occurrence is not None
                        and item.occurrence.provider.startswith("tool:")
                    ):
                        targets.append(item)
                if len(targets) == 1:
                    kind, action = "accepted_tool_decision", targets[0]
                    support.add(action.event.key)
                else:
                    uncertainties.append("接受事件缺少唯一的已核验工具请求")
        else:
            uncertainties.append("模型输出未关联唯一的已核验接受事件")
        if key in ambiguous:
            uncertainties.append("参考轨迹中的对应关系仍有歧义")
        affected = tuple(index for index, path in enumerate(failures) if key in path)
        distance = min((failures[index][key] for index in affected), default=None)
        priority = 0
        if report_errors:
            artifact = (
                action is not None
                and action.occurrence is not None
                and action.occurrence.provider == "tool:artifact_write/v1"
            )
            if "missing_artifacts" in report_errors:
                priority = 0 if kind == "accepted_plan" else 1 if artifact else 2
            else:
                priority = 0 if artifact else 1 if kind == "accepted_plan" else 2
        reasons = [
            "处于失败断言的已观察依赖路径" if affected else "完整候选回退：尚无已核验失败依赖路径"
        ]
        if report_errors:
            reasons.append("报告硬断言优先检查计划遗漏或产物生成决策，输入观测单列为证据")
        if key in divergence:
            reasons.append("首个可见分歧距离来自依赖图，不使用到达顺序")
        candidates.append(
            FaultCandidate(
                event=node.event,
                kind=kind,
                action_step=action.logical_step_id if action else None,
                action_provider=action.occurrence.provider
                if action and action.occurrence
                else None,
                supporting_events=tuple(nodes[item].event for item in sorted(support)),
                assertion_indexes=affected,
                features=CandidateFeatures(
                    failure_path=bool(affected),
                    failure_distance=distance,
                    divergence_distance=divergence.get(key),
                    report_role_priority=priority,
                    uncertainty_count=len(uncertainties),
                ),
                rank=1,
                tied_count=1,
                reasons=tuple(reasons),
                uncertainties=tuple(uncertainties),
            )
        )

    def score(candidate: FaultCandidate) -> tuple[int, ...]:
        feature = candidate.features
        absent = len(nodes) + 1
        return (
            0 if feature.failure_path else 1,
            feature.report_role_priority,
            feature.failure_distance if feature.failure_distance is not None else absent,
            feature.divergence_distance if feature.divergence_distance is not None else absent,
            feature.uncertainty_count,
        )

    groups: dict[tuple[int, ...], list[FaultCandidate]] = defaultdict(list)
    for candidate in candidates:
        groups[score(candidate)].append(candidate)
    ranked: list[FaultCandidate] = []
    for features in sorted(groups):
        tied = groups[features]
        rank = len(ranked) + 1
        # 此排序只稳定展示；并列项保留相同竞争排名，不以 ID 决定 Top-k。
        ranked.extend(
            evolve(item, rank=rank, tied_count=len(tied))
            for item in sorted(tied, key=lambda item: item.event.key)
        )
    anomalies = {
        node.event.key: node.event
        for node in graph.nodes
        if node.occurrence is not None
        and node.occurrence.provider.startswith("tool:")
        and (node.error_code is not None or node.event.key in frontier)
    }
    limitations = [
        "静态排序是待检验候选，不是因果分数或已证实根因。",
        "未运行干预试验，改善率和修复效果未知。",
        "输入/工具异常与可修复决策分开；接受事件不证明替换决策一定可修复。",
        "并列保留竞争排名，完整候选列表不按 Top-k 截断。",
    ]
    if comparison is None or comparison.status != "comparable":
        limitations.append("没有可用的共同祖先成功参考，仅沿硬断言与已核验依赖静态提取。")
    if not assertions:
        limitations.append("没有已核验的失败硬断言，候选仅供完整列表回退，不能宣称完成定位。")
    has_path = any(item.features.failure_path for item in ranked)
    if assertions and not has_path:
        limitations.append("没有候选位于已核验失败依赖路径，保留完整回退列表而不宣称已定位。")
    complete = (
        graph.scope_complete
        and bool(assertions)
        and has_path
        and not graph.gaps
        and all(node.payload_verified for node in graph.nodes)
    )
    if comparison is not None and not comparison.complete_evidence:
        complete = False
        limitations.append("参考对齐证据不完整或有歧义，首个分歧及依赖范围不保证完整。")
    return StaticDiagnosis(
        status="candidates_available" if assertions and has_path else "insufficient_evidence",
        rules_digest=RULES_DIGEST,
        assertions=assertions,
        candidates=tuple(ranked),
        observed_anomalies=tuple(anomalies[key] for key in sorted(anomalies)),
        excluded_terminal_outputs=tuple(sorted(excluded, key=lambda item: item.key)),
        complete_evidence=complete,
        limitations=tuple(limitations),
    )
