"""静态定位的硬断言、候选与不可变报告，不接收样例标签或自报根因。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.diagnosis import (
    BehavioralDiff,
    EventReference,
    Positive,
    Text,
    TraceGraph,
)
from agentgit.core.models import Contract, Digest, Identifier, NonNegative

MAX_ASSERTIONS = 64
CandidateKind = Literal["accepted_plan", "accepted_tool_decision", "unaccepted_model_output"]


class NewDebugReport(Contract):
    failed_run_id: Identifier
    reference_run_id: Identifier | None = None


class FailureAssertion(Contract):
    event: EventReference
    origin: Literal["research_validator", "runtime_stop"]
    code: Text


class CandidateFeatures(Contract):
    failure_path: bool
    failure_distance: NonNegative | None
    divergence_distance: NonNegative | None
    report_role_priority: Annotated[int, Field(ge=0, le=2)]
    uncertainty_count: NonNegative


class FaultCandidate(Contract):
    event: EventReference
    kind: CandidateKind
    action_step: Text | None
    action_provider: Text | None
    supporting_events: tuple[EventReference, ...]
    assertion_indexes: tuple[NonNegative, ...]
    features: CandidateFeatures
    rank: Positive
    tied_count: Positive
    reasons: tuple[str, ...]
    uncertainties: tuple[str, ...]


class StaticDiagnosis(Contract):
    mode: Literal["static_only"] = "static_only"
    status: Literal["candidates_available", "insufficient_evidence"]
    rules_version: Literal["static-failure-path/v1"] = "static-failure-path/v1"
    rules_digest: Digest
    assertions: Annotated[tuple[FailureAssertion, ...], Field(max_length=MAX_ASSERTIONS)]
    candidates: tuple[FaultCandidate, ...]
    observed_anomalies: tuple[EventReference, ...]
    excluded_terminal_outputs: tuple[EventReference, ...]
    complete_evidence: bool
    intervention_status: Literal["not_run"] = "not_run"
    intervention_improvement: None = None
    limitations: tuple[str, ...]


class DebugReport(Contract):
    kind: Literal["debug_report/v1"] = "debug_report/v1"
    repository_id: Identifier
    failed: TraceGraph
    reference: TraceGraph | None
    comparison: BehavioralDiff | None
    environment_changes: tuple[str, ...]
    diagnosis: StaticDiagnosis
    source_revision: Text

    @model_validator(mode="after")
    def bound_evidence(self) -> "DebugReport":
        if self.failed.repository_id != self.repository_id or (
            self.reference and self.reference.repository_id != self.repository_id
        ):
            raise ValueError("报告轨迹必须属于同一项目")
        if (self.reference is None) != (self.comparison is None):
            raise ValueError("参考轨迹与比较结果必须成对提供")
        if self.reference is not None and self.comparison is not None:
            comparison = self.comparison
            if comparison.status == "comparable" and (
                comparison.common_ancestor is None
                or comparison.common_ancestor != self.failed.anchor_commit
                or comparison.common_ancestor != self.reference.anchor_commit
            ):
                raise ValueError("报告比较必须绑定双方共同祖先")
            sides = (
                (
                    self.failed,
                    [item.left for item in comparison.matches],
                    comparison.left_only,
                    comparison.first_left,
                    [ref for item in comparison.ambiguities for ref in item.left],
                ),
                (
                    self.reference,
                    [item.right for item in comparison.matches],
                    comparison.right_only,
                    comparison.first_right,
                    [ref for item in comparison.ambiguities for ref in item.right],
                ),
            )
            for graph, matched, only, first, ambiguous in sides:
                available = {node.event.key: node.event for node in graph.nodes}
                if any(
                    available.get(ref.key) != ref for ref in (*matched, *only, *first, *ambiguous)
                ):
                    raise ValueError("报告比较引用不属于对应轨迹")
        events = {node.event.key: node.event for node in self.failed.nodes}
        refs = [item.event for item in self.diagnosis.assertions]
        refs.extend(self.diagnosis.observed_anomalies)
        refs.extend(self.diagnosis.excluded_terminal_outputs)
        for candidate in self.diagnosis.candidates:
            refs.extend((candidate.event, *candidate.supporting_events))
            if any(
                index >= len(self.diagnosis.assertions) for index in candidate.assertion_indexes
            ):
                raise ValueError("候选引用不存在的失败断言")
        if any(events.get(ref.key) != ref for ref in refs):
            raise ValueError("候选与证据必须引用失败侧的精确事件")
        if len({item.event.key for item in self.diagnosis.candidates}) != len(
            self.diagnosis.candidates
        ):
            raise ValueError("候选决策不能重复")
        counts: dict[int, int] = {}
        for item in self.diagnosis.candidates:
            counts[item.rank] = counts.get(item.rank, 0) + 1
        position = 1
        for rank, count in sorted(counts.items()):
            if rank != position:
                raise ValueError("候选并列竞争排名不完整")
            position += count
        if any(item.tied_count != counts[item.rank] for item in self.diagnosis.candidates):
            raise ValueError("候选并列数量与排名不符")
        return self
