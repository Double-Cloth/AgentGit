"""候选只沿同 occurrence 显式边绑定，缺口及歧义均不能自动干预。"""

import pytest

from agentgit.core.debug_reports import DebugReport, FailureAssertion
from agentgit.core.diagnosis import (
    DependencyEdge,
    EventReference,
    EvidenceGap,
    TraceGraph,
    TraceNode,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallOccurrence
from agentgit.core.research import evolve
from agentgit.diagnosis.candidates import static_diagnosis
from agentgit.diagnosis.interventions import candidate_request

REPO = "00000000-0000-4000-8000-000000000001"
RUN = "00000000-0000-4000-8000-000000000002"


def bound_report(change=None):
    occurrence = CallOccurrence(
        logical_step_id="model-plan",
        call_index=0,
        attempt=1,
        provider="llm:mock-research/v1",
        signature_digest="a" * 64,
        request_digest="b" * 64,
    )
    nodes = [
        TraceNode(
            event=EventReference(run_id=RUN, seq=seq, event_id=100 + seq),
            event_type=kind,
            logical_step_id=occurrence.logical_step_id if seq <= 3 else kind,
            phase="call.request" if seq <= 2 else "call.result" if seq == 3 else kind,
            payload_ref="c" * 64,
            occurrence=occurrence if seq <= 3 else None,
            observation_ref="d" * 64 if seq == 3 else None,
        )
        for seq, kind in enumerate(
            ("llm.requested", "llm.requested", "llm.completed", "plan.accepted", "run.evaluated"), 1
        )
    ]
    edges = [
        DependencyEdge(source=nodes[a].event, target=nodes[b].event, basis=basis)
        for a, b, basis in (
            (0, 2, "call_occurrence"),
            (2, 3, "decision_context"),
            (3, 4, "plan_dependency"),
        )
    ]
    gaps = []
    if change == "missing_edge":
        edges = edges[1:]
    elif change == "two_requests":
        edges.append(
            DependencyEdge(source=nodes[1].event, target=nodes[2].event, basis="call_occurrence")
        )
    elif change in ("request_gap", "result_gap"):
        gaps = [
            EvidenceGap(
                event=nodes[0 if change == "request_gap" else 2].event, reason="missing_payload"
            )
        ]
    elif change == "unverified":
        nodes[2] = evolve(nodes[2], payload_verified=False)
    elif change == "no_observation":
        nodes[2] = evolve(nodes[2], observation_ref=None)
    elif change == "mismatch":
        nodes[0] = evolve(nodes[0], occurrence=evolve(occurrence, request_digest="e" * 64))
    elif change == "wrong_kind":
        nodes[0] = evolve(nodes[0], event_type="tool.started")
    elif change == "no_id":
        nodes[0] = evolve(nodes[0], event=evolve(nodes[0].event, event_id=None))
        edges[0] = evolve(edges[0], source=nodes[0].event)
    graph = TraceGraph(
        repository_id=REPO,
        run_id=RUN,
        anchor_commit="f" * 64,
        run_status="failed",
        as_of_seq=5,
        nodes=tuple(nodes),
        edges=tuple(edges),
        gaps=tuple(gaps),
    )
    return DebugReport(
        repository_id=REPO,
        failed=graph,
        reference=None,
        comparison=None,
        environment_changes=(),
        source_revision="unit-test",
        diagnosis=static_diagnosis(
            graph,
            (
                FailureAssertion(
                    event=nodes[-1].event, origin="research_validator", code="missing_artifacts"
                ),
            ),
        ),
    )


def test_explicit_occurrence_binds_result_to_request_without_using_adjacent_event():
    report = bound_report()
    candidate, request = candidate_request(report, 103)
    assert candidate.kind == "accepted_plan" and request.event.event_id == 101
    assert report.diagnosis.intervention_improvement is None


@pytest.mark.parametrize(
    "change",
    [
        "missing_edge",
        "two_requests",
        "request_gap",
        "result_gap",
        "unverified",
        "no_observation",
        "mismatch",
        "wrong_kind",
        "no_id",
    ],
)
def test_missing_ambiguous_or_unverified_call_is_rejected(change):
    with pytest.raises(AgentGitError) as error:
        candidate_request(bound_report(change), 103)
    assert error.value.code == ErrorCode.RECORDING_MISSING


@pytest.mark.parametrize("value", [True, 0, -1, "103", 9_007_199_254_740_992])
def test_candidate_event_id_is_strictly_bounded(value):
    with pytest.raises(AgentGitError) as error:
        candidate_request(bound_report(), value)
    assert error.value.code == ErrorCode.VALIDATION


def test_request_or_terminal_event_cannot_masquerade_as_candidate():
    with pytest.raises(AgentGitError) as error:
        candidate_request(bound_report(), 101)
    assert error.value.code == ErrorCode.NOT_FOUND
