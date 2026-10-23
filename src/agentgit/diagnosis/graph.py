"""只从已验证快照提取计划/决策边，不从到达顺序推导因果。"""

from collections import defaultdict

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.diagnosis import DependencyEdge, EvidenceGap, TraceNode
from agentgit.core.research import AgentState, CallTrace


def contextual_edges(
    nodes: tuple[TraceNode, ...],
    states: dict[str, AgentState],
    state_events: dict[str, str],
    call_checkpoints: dict[str, str],
    verified_model_contexts: set[str],
) -> tuple[list[DependencyEdge], list[EvidenceGap]]:
    by_key = {node.event.key: node for node in nodes}
    results: dict[tuple[str, int, int, str, str, str, str], list[TraceNode]] = defaultdict(list)
    for node in nodes:
        call = node.occurrence
        if (
            call is not None
            and node.phase == "call.result"
            and node.observation_ref
            and node.payload_verified
        ):
            results[
                (
                    call.logical_step_id,
                    call.call_index,
                    call.attempt,
                    call.provider,
                    call.signature_digest,
                    call.request_digest,
                    node.observation_ref,
                )
            ].append(node)
    edges: list[DependencyEdge] = []
    gaps: list[EvidenceGap] = []

    def result(trace: CallTrace) -> TraceNode | None:
        found = results.get(
            (
                trace.logical_step_id,
                trace.call_index,
                trace.attempt,
                trace.provider,
                trace.signature_digest,
                trace.request_ref,
                trace.response_ref,
            ),
            [],
        )
        return found[0] if len(found) == 1 and found[0].error_code == trace.error_code else None

    for checkpoint, state in states.items():
        event = by_key.get(state_events[checkpoint])
        if (
            event is not None
            and state.messages
            and event.event_type
            in {
                "plan.accepted",
                "decision.accepted",
                "tool.observed",
                "run.evaluated",
                "run.replanning",
                "run.stopped",
                "run.outcome_unknown",
            }
        ):
            source = result(state.messages[-1])
            if source is not None and source.event.key != event.event.key:
                edges.append(
                    DependencyEdge(
                        source=source.event, target=event.event, basis="decision_context"
                    )
                )
            elif source is None:
                gaps.append(EvidenceGap(event=event.event, reason="outside_trace"))
    for node in nodes:
        if node.phase != "call.request" or node.occurrence is None:
            continue
        context_checkpoint = call_checkpoints.get(node.event.key)
        context_state = states.get(context_checkpoint or "")
        if context_state is None:
            gaps.append(EvidenceGap(event=node.event, reason="outside_trace"))
            continue
        context_event = by_key.get(state_events[context_checkpoint or ""])
        if node.occurrence.provider.startswith("llm:"):
            if node.event.key in verified_model_contexts and context_event is not None:
                edges.append(
                    DependencyEdge(
                        source=context_event.event, target=node.event, basis="decision_context"
                    )
                )
            else:
                gaps.append(EvidenceGap(event=node.event, reason="unverified_context"))
            continue
        if context_state.pending_tool is None:
            gaps.append(EvidenceGap(event=node.event, reason="unverified_context"))
            continue
        decision, call = context_state.pending_tool, node.occurrence
        expected_request = object_digest(ObjectType.BLOB, encode(decision.arguments))
        if (
            decision.node_key != call.logical_step_id
            or call.provider != f"tool:{decision.tool}/v1"
            or expected_request != call.request_digest
        ):
            gaps.append(EvidenceGap(event=node.event, reason="unverified_context"))
            continue
        if context_event is not None:
            edges.append(
                DependencyEdge(
                    source=context_event.event, target=node.event, basis="decision_context"
                )
            )
        else:
            gaps.append(EvidenceGap(event=node.event, reason="outside_trace"))
        latest = next(
            (
                trace
                for trace in reversed(context_state.messages)
                if trace.provider.startswith("llm:")
            ),
            None,
        )
        if latest is not None:
            source = result(latest)
            if source is not None:
                edges.append(
                    DependencyEdge(source=source.event, target=node.event, basis="decision_context")
                )
            else:
                gaps.append(EvidenceGap(event=node.event, reason="outside_trace"))
        if context_state.plan is None:
            gaps.append(EvidenceGap(event=node.event, reason="unverified_context"))
            continue
        planned = next(
            (item for item in context_state.plan.nodes if item.key == decision.node_key), None
        )
        if planned is None:
            gaps.append(EvidenceGap(event=node.event, reason="unverified_context"))
            continue
        for dependency in planned.dependencies:
            trace = next(
                (
                    trace
                    for trace in reversed(context_state.messages)
                    if trace.logical_step_id == dependency and trace.error_code is None
                ),
                None,
            )
            source = result(trace) if trace is not None else None
            if dependency not in context_state.completed or source is None:
                gaps.append(EvidenceGap(event=node.event, reason="outside_trace"))
            else:
                edges.append(
                    DependencyEdge(source=source.event, target=node.event, basis="plan_dependency")
                )
    return edges, gaps
