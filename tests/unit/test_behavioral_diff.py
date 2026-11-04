"""对齐使用逻辑身份和依赖；重排、缺证据和歧义不能变成确定因果。"""

from itertools import permutations

import pytest
from pydantic import ValidationError

from agentgit.core.diagnosis import (
    DependencyEdge,
    EventReference,
    EvidenceGap,
    TraceGraph,
    TraceNode,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallOccurrence
from agentgit.diff.behavioral import behavioral_diff

REPO = "00000000-0000-4000-8000-000000000001"
LEFT = "00000000-0000-4000-8000-000000000002"
RIGHT = "00000000-0000-4000-8000-000000000003"
ANCHOR = "a" * 64


def node(run, seq, step, value=1, *, phase="state", call=None, verified=True):
    return TraceNode(
        event=EventReference(run_id=run, seq=seq, event_id=10000 + seq),
        event_type="state.observed",
        logical_step_id=step,
        phase=phase,
        payload_ref=f"{value:064x}",
        occurrence=call,
        payload_verified=verified,
    )


def graph(nodes, links=(), *, gaps=(), anchor=ANCHOR, repository=REPO):
    return TraceGraph(
        repository_id=repository,
        run_id=nodes[0].event.run_id if nodes else LEFT,
        anchor_commit=anchor,
        as_of_seq=max((item.event.seq for item in nodes), default=0),
        nodes=tuple(nodes),
        edges=tuple(
            DependencyEdge(source=a.event, target=b.event, basis="caused_by") for a, b in links
        ),
        gaps=tuple(gaps),
    )


@pytest.mark.parametrize("order", list(permutations((1, 2, 3))))
def test_arrival_seq_event_ids_and_container_order_do_not_change_alignment(order):
    a, b, c = [
        node(LEFT, index, step) for index, step in enumerate(("search", "read", "report"), 1)
    ]
    x, y, z = [
        node(RIGHT, index + 10, step)
        for index, step in zip(order, ("search", "read", "report"), strict=True)
    ]
    result = behavioral_diff(graph([a, b, c], [(a, c), (b, c)]), graph([z, y, x], [(x, z), (y, z)]))
    assert result.complete_evidence
    assert all(item.comparison == "equal" for item in result.matches)
    assert not result.left_only and not result.right_only and not result.first_left
    names_left = {item.event.key: item.logical_step_id for item in [a, b, c]}
    names_right = {item.event.key: item.logical_step_id for item in [x, y, z]}
    assert {
        (names_left[item.left.key], names_right[item.right.key]) for item in result.matches
    } == {(key, key) for key in ("search", "read", "report")}


def test_same_arrival_order_with_different_steps_is_not_a_match():
    result = behavioral_diff(graph([node(LEFT, 1, "search")]), graph([node(RIGHT, 1, "unrelated")]))
    assert not result.matches
    assert len(result.left_only) == len(result.right_only) == 1


def test_symmetric_repeated_identity_remains_ambiguous_even_with_different_ids():
    result = behavioral_diff(
        graph([node(LEFT, 1, "repeat"), node(LEFT, 2, "repeat")]),
        graph([node(RIGHT, 9, "repeat"), node(RIGHT, 4, "repeat")]),
    )
    assert not result.matches and len(result.ambiguities) == 1
    assert len(result.ambiguities[0].left) == len(result.ambiguities[0].right) == 2
    assert not result.complete_evidence


def test_repeated_identity_is_resolved_by_verified_dependency_context():
    a, b, c, d = [
        node(LEFT, index, step) for index, step in enumerate(("a", "b", "repeat", "repeat"), 1)
    ]
    x, y, z, w = [
        node(RIGHT, index, step) for index, step in enumerate(("b", "a", "repeat", "repeat"), 10)
    ]
    result = behavioral_diff(
        graph([a, b, c, d], [(a, c), (b, d)]), graph([x, y, z, w], [(y, w), (x, z)])
    )
    pairs = {item.left.key: item.right.key for item in result.matches}
    assert pairs[c.event.key] == w.event.key and pairs[d.event.key] == z.event.key
    assert result.complete_evidence and not result.ambiguities
    assert [item.basis for item in result.matches if item.left in (c.event, d.event)] == [
        "dependency_context",
        "dependency_context",
    ]


def test_repeated_roots_can_use_unique_canonical_content():
    result = behavioral_diff(
        graph([node(LEFT, 1, "repeat", 1), node(LEFT, 2, "repeat", 2)]),
        graph([node(RIGHT, 3, "repeat", 2), node(RIGHT, 4, "repeat", 1)]),
    )
    assert {(item.left.seq, item.right.seq, item.basis) for item in result.matches} == {
        (1, 4, "canonical_content"),
        (2, 3, "canonical_content"),
    }


def test_first_divergence_is_a_dag_frontier_including_parallel_nodes():
    a, b, c, d = [
        node(LEFT, index, step) for index, step in enumerate(("root", "left", "right", "final"), 1)
    ]
    x, y, z, w = [
        node(RIGHT, index, step, 2 if step != "root" else 1)
        for index, step in enumerate(("root", "right", "left", "final"), 7)
    ]
    result = behavioral_diff(
        graph([a, b, c, d], [(a, b), (a, c), (b, d), (c, d)]),
        graph([x, y, z, w], [(x, y), (x, z), (y, w), (z, w)]),
    )
    assert set(result.first_left) == {b.event, c.event}
    assert set(result.first_right) == {y.event, z.event}
    assert len([item for item in result.matches if item.comparison == "changed"]) == 3


def test_changed_dependency_is_visible_for_unique_logical_identity():
    a, b = node(LEFT, 1, "a"), node(LEFT, 2, "b")
    x, y = node(RIGHT, 3, "a"), node(RIGHT, 4, "b")
    result = behavioral_diff(graph([a, b], [(a, b)]), graph([x, y]))
    assert next(item for item in result.matches if item.left == b.event).differences == (
        "dependencies",
    )


def test_call_request_signature_observation_and_error_have_separate_differences():
    occurrence = CallOccurrence(
        logical_step_id="read",
        provider="tool:source_read/v1",
        call_index=0,
        attempt=1,
        signature_digest="b" * 64,
        request_digest="c" * 64,
    )
    original = node(LEFT, 1, "read", call=occurrence, phase="call.result")
    original = original.model_copy(update={"observation_ref": "d" * 64})
    changed = TraceNode.model_validate(
        {
            **original.model_dump(),
            "event": EventReference(run_id=RIGHT, seq=2),
            "event_type": "call.failed",
            "occurrence": CallOccurrence(
                **{
                    **occurrence.model_dump(),
                    "signature_digest": "e" * 64,
                    "request_digest": "f" * 64,
                }
            ),
            "observation_ref": "a" * 64,
            "error_code": "tool_error",
        }
    )
    result = behavioral_diff(graph([original]), graph([changed]))
    assert result.matches[0].differences == (
        "event_type",
        "signature",
        "request",
        "observation",
        "error",
    )


def test_missing_payload_never_becomes_equal_or_complete():
    a = node(LEFT, 1, "read", verified=False)
    gap = EvidenceGap(event=a.event, reason="missing_payload")
    result = behavioral_diff(graph([a], gaps=[gap]), graph([node(RIGHT, 2, "read")]))
    assert result.matches[0].comparison == "unverified" and not result.complete_evidence
    assert not result.first_left
    unmatched = behavioral_diff(graph([a]), graph([node(RIGHT, 2, "other")]))
    assert not unmatched.complete_evidence


def test_no_common_ancestor_abstains_and_cross_project_is_rejected():
    a, b = graph([node(LEFT, 1, "a")]), graph([node(RIGHT, 1, "a")], anchor="b" * 64)
    assert behavioral_diff(a, b).status == "no_shared_ancestor"
    with pytest.raises(AgentGitError) as error:
        behavioral_diff(a, graph([node(RIGHT, 1, "a")], repository=RIGHT))
    assert error.value.code == ErrorCode.NOT_FOUND


def test_graph_rejects_cycles_duplicate_nodes_wrong_endpoints_and_gap_ids():
    a, b = node(LEFT, 1, "a"), node(LEFT, 2, "b")
    with pytest.raises(ValidationError, match="循环"):
        graph([a, b], [(a, b), (b, a)])
    with pytest.raises(ValidationError, match="重复"):
        graph([a, a])
    with pytest.raises(ValidationError, match="精确事件"):
        graph([a], [(a, b)])
    with pytest.raises(ValidationError, match="图内事件"):
        graph(
            [a],
            gaps=[
                EvidenceGap(
                    event=EventReference(run_id=LEFT, seq=1, event_id=999), reason="missing_payload"
                )
            ],
        )


def test_bounded_duplicate_alignment_fails_instead_of_returning_a_prefix(monkeypatch):
    monkeypatch.setattr("agentgit.diff.behavioral.MAX_COMPARISONS", 3)
    with pytest.raises(AgentGitError) as error:
        behavioral_diff(
            graph([node(LEFT, 1, "a"), node(LEFT, 2, "a")]),
            graph([node(RIGHT, 1, "a"), node(RIGHT, 2, "a")]),
        )
    assert error.value.code == ErrorCode.OUTPUT_LIMIT
