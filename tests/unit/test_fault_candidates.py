"""静态候选的路径、角色、并列及证据边界；不读取数据集标签。"""

from itertools import pairwise

import pytest
from pydantic import ValidationError

from agentgit.core.debug_reports import DebugReport, FailureAssertion, NewDebugReport
from agentgit.core.diagnosis import (
    BehavioralDiff,
    DependencyEdge,
    EventReference,
    EvidenceGap,
    TraceGraph,
    TraceNode,
)
from agentgit.core.errors import AgentGitError
from agentgit.core.models import CallOccurrence
from agentgit.core.research import evolve
from agentgit.diagnosis.candidates import static_diagnosis

REPO = "00000000-0000-4000-8000-000000000001"
RUN = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"


def event(seq, kind, step=None, provider=None, phase=None, error=None):
    logical = step or f"state-{seq}"
    return TraceNode(
        event=EventReference(run_id=RUN, seq=seq, event_id=100 + seq),
        event_type=kind,
        logical_step_id=logical,
        phase=phase or ("call.result" if provider and provider.startswith("llm:") else kind),
        payload_ref=f"{seq:064x}",
        occurrence=CallOccurrence(
            logical_step_id=logical,
            call_index=0,
            attempt=1,
            provider=provider,
            signature_digest="b" * 64,
            request_digest="c" * 64,
        )
        if provider
        else None,
        observation_ref="d" * 64 if provider and phase != "call.request" else None,
        error_code=error,
    )


def edge(a, b, basis="decision_context"):
    return DependencyEdge(source=a.event, target=b.event, basis=basis)


def graph(nodes, edges=(), gaps=()):
    return TraceGraph(
        repository_id=REPO,
        run_id=RUN,
        anchor_commit="a" * 64,
        head_commit="f" * 64,
        run_status="failed",
        as_of_seq=max(node.event.seq for node in nodes),
        nodes=tuple(nodes),
        edges=tuple(edges),
        gaps=tuple(gaps),
    )


def report_graph():
    plan = event(1, "llm.completed", "model-plan", "llm:mock/v1")
    accepted_plan = event(2, "plan.accepted")
    read = event(3, "llm.completed", "model-read", "llm:mock/v1")
    accepted_read = event(4, "decision.accepted")
    request_read = event(5, "tool.started", "read", "tool:source_read/v1", "call.request")
    observed_read = event(6, "tool.completed", "read", "tool:source_read/v1", "call.result")
    write = event(7, "llm.completed", "model-write", "llm:mock/v1")
    accepted_write = event(8, "decision.accepted")
    request_write = event(
        9, "tool.started", "write-report", "tool:artifact_write/v1", "call.request"
    )
    observed_write = event(
        10, "tool.completed", "write-report", "tool:artifact_write/v1", "call.result"
    )
    final = event(11, "llm.completed", "model-final", "llm:mock/v1")
    evaluated = event(12, "run.evaluated")
    nodes = [
        plan,
        accepted_plan,
        read,
        accepted_read,
        request_read,
        observed_read,
        write,
        accepted_write,
        request_write,
        observed_write,
        final,
        evaluated,
    ]
    links = [edge(a, b) for a, b in pairwise(nodes)]
    return graph(nodes, links), evaluated, plan, read, write, final


def assertion(node, code="source_expired:source", origin="research_validator"):
    return FailureAssertion(event=node.event, origin=origin, code=code)


def test_without_reference_report_failure_prioritizes_artifact_decision_and_keeps_input_separate():
    trace, failed, plan, read, write, final = report_graph()
    result = static_diagnosis(trace, (assertion(failed),))
    assert result.status == "candidates_available" and result.complete_evidence
    assert result.mode == "static_only" and result.intervention_improvement is None
    assert result.candidates[0].event == write.event
    assert result.candidates[0].kind == "accepted_tool_decision"
    assert result.candidates[0].action_step == "write-report"
    assert {item.event.key for item in result.candidates} == {
        plan.event.key,
        read.event.key,
        write.event.key,
    }
    assert result.excluded_terminal_outputs == (final.event,)
    assert any("没有可用" in text for text in result.limitations)


def test_missing_artifact_hard_assertion_keeps_omitted_plan_as_candidate():
    trace, failed, plan, _, _, _ = report_graph()
    result = static_diagnosis(trace, (assertion(failed, "missing_artifacts"),))
    assert result.candidates[0].event == plan.event
    assert result.candidates[0].kind == "accepted_plan"


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_evidence_preserves_competition_ties_instead_of_event_order(reverse):
    a = event(1, "call.failed", "a", "llm:mock/v1")
    b = event(2, "call.failed", "b", "llm:mock/v1")
    end = event(3, "run.stopped")
    nodes = [a, b, end]
    result = static_diagnosis(
        graph(list(reversed(nodes)) if reverse else nodes, [edge(a, end), edge(b, end)]),
        (assertion(end, "model_error", "runtime_stop"),),
    )
    assert len(result.candidates) == 2
    assert all(item.rank == 1 and item.tied_count == 2 for item in result.candidates)


def test_arrival_relabeling_changes_only_address_not_features_or_ranks():
    original, end, *_ = report_graph()
    remapped = {
        node.event.key: evolve(node.event, seq=100 - node.event.seq, event_id=2000 + node.event.seq)
        for node in original.nodes
    }
    changed = evolve(
        original,
        as_of_seq=100,
        nodes=tuple(
            evolve(node, event=remapped[node.event.key]) for node in reversed(original.nodes)
        ),
        edges=tuple(
            evolve(item, source=remapped[item.source.key], target=remapped[item.target.key])
            for item in original.edges
        ),
    )
    a = static_diagnosis(original, (assertion(end),))
    b = static_diagnosis(changed, (evolve(assertion(end), event=remapped[end.event.key]),))
    assert [(item.kind, item.action_step, item.rank, item.features) for item in a.candidates] == [
        (item.kind, item.action_step, item.rank, item.features) for item in b.candidates
    ]


def test_tool_anomaly_is_evidence_and_does_not_become_a_model_decision():
    trace, failed, _, read, _, _ = report_graph()
    tool = next(
        node
        for node in trace.nodes
        if node.logical_step_id == "read" and node.phase == "call.result"
    )
    comparison = BehavioralDiff(
        status="comparable",
        common_ancestor=trace.anchor_commit,
        first_left=(tool.event,),
        complete_evidence=True,
    )
    result = static_diagnosis(trace, (assertion(failed),), comparison)
    assert result.observed_anomalies == (tool.event,)
    assert tool.event.key not in {item.event.key for item in result.candidates}
    assert (
        next(
            item for item in result.candidates if item.event == read.event
        ).features.divergence_distance
        is not None
    )


def test_missing_accepted_event_payload_keeps_model_role_uncertain():
    trace, failed, _, _, _, _ = report_graph()
    target = next(node for node in trace.nodes if node.event.seq == 8)
    trace = evolve(
        trace,
        nodes=tuple(
            evolve(node, payload_verified=False) if node == target else node for node in trace.nodes
        ),
        gaps=(EvidenceGap(event=target.event, reason="missing_payload"),),
    )
    result = static_diagnosis(trace, (assertion(failed),))
    write = next(item for item in result.candidates if item.event.seq == 7)
    assert write.kind == "unaccepted_model_output" and write.uncertainties
    assert not result.complete_evidence


def test_no_hard_assertions_is_full_fallback_with_unknown_effect_not_verified_localization():
    trace, *_ = report_graph()
    result = static_diagnosis(trace, ())
    assert result.status == "insufficient_evidence" and result.candidates
    assert not result.complete_evidence and all(
        not item.features.failure_path for item in result.candidates
    )
    assert result.intervention_status == "not_run" and result.intervention_improvement is None


def test_disconnected_failure_has_fallback_candidates_but_no_verified_location():
    trace, failed, *_ = report_graph()
    trace = evolve(trace, edges=())
    result = static_diagnosis(trace, (assertion(failed),))
    assert result.candidates and result.status == "insufficient_evidence"
    assert not result.complete_evidence and all(
        not item.features.failure_path for item in result.candidates
    )


@pytest.mark.parametrize(
    "problem", ["foreign", "wrong_id", "unverified", "duplicate", "limit", "comparison"]
)
def test_assertion_and_comparison_evidence_bounds_reject_untrusted_inputs(problem):
    trace, failed, *_ = report_graph()
    item = assertion(failed)
    assertions, comparison = (item,), None
    if problem == "foreign":
        assertions = (evolve(item, event=evolve(item.event, run_id=OTHER)),)
    elif problem == "wrong_id":
        assertions = (evolve(item, event=evolve(item.event, event_id=1)),)
    elif problem == "unverified":
        trace = evolve(
            trace,
            nodes=tuple(
                evolve(node, payload_verified=False) if node == failed else node
                for node in trace.nodes
            ),
        )
    elif problem == "duplicate":
        assertions = (item, item)
    elif problem == "limit":
        assertions = (item,) * 65
    else:
        comparison = BehavioralDiff(
            status="comparable",
            common_ancestor=trace.anchor_commit,
            first_left=(evolve(item.event, run_id=OTHER),),
            complete_evidence=True,
        )
    with pytest.raises(AgentGitError):
        static_diagnosis(trace, assertions, comparison)


def test_request_does_not_accept_labels_or_fake_hard_assertions():
    with pytest.raises(ValidationError):
        NewDebugReport(failed_run_id=RUN, gold_event=1)


def test_report_rejects_cross_project_and_wrong_candidate_evidence():
    trace, failed, *_ = report_graph()
    diagnosis = static_diagnosis(trace, (assertion(failed),))
    report = DebugReport(
        repository_id=REPO,
        failed=trace,
        reference=None,
        comparison=None,
        environment_changes=(),
        diagnosis=diagnosis,
        source_revision="fixture",
    )
    with pytest.raises(ValidationError):
        evolve(report, repository_id=OTHER)
    candidate = evolve(
        diagnosis.candidates[0], event=evolve(diagnosis.candidates[0].event, event_id=1)
    )
    with pytest.raises(ValidationError):
        evolve(
            report, diagnosis=evolve(diagnosis, candidates=(candidate, *diagnosis.candidates[1:]))
        )
