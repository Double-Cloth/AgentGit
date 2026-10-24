"""重算不可变删除轨迹；成功删除后重启本轮，旧失败不能证明新集合必要性。"""

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_validation import (
    DeletionRequest,
    DeletionStep,
    FinalRepairLock,
    ReductionState,
    ReductionSummary,
    RepairCandidate,
    ValidationSample,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import evolve


def _request(
    current: RepairCandidate,
    tested: dict[str, ValidationTrialEvidence],
    sample: ValidationSample,
) -> DeletionRequest | None:
    atom = next((atom for atom in current.atoms if atom.patch_ref not in tested), None)
    if atom is None:
        return None
    after = evolve(current, atoms=tuple(a for a in current.atoms if a.patch_ref != atom.patch_ref))
    return DeletionRequest(
        before_ref=validation_ref(current),
        removed_patch_ref=atom.patch_ref,
        after=after,
        sample_ref=validation_ref(sample),
        validator_ref=sample.validator_ref,
    )


def _replay(
    state: ReductionState,
) -> tuple[RepairCandidate, dict[str, ValidationTrialEvidence], bool]:
    current, halted = state.initial, False
    tested: dict[str, ValidationTrialEvidence] = {}
    runs, results, trials = set(state.screening_run_ids), set(state.screening_result_refs), set()
    prefix = evolve(state, steps=())
    for step in state.steps:
        expected = _request(current, tested, state.sample)
        request = DeletionRequest.model_validate(step.model_dump(exclude={"evidence"}))
        evidence = step.evidence
        if (
            halted
            or request != expected
            or (
                evidence.phase != "reduce"
                or evidence.protocol_ref != validation_ref(prefix)
                or evidence.candidate_ref != validation_ref(step.after)
                or evidence.sample_ref != step.sample_ref
                or evidence.validator_ref != step.validator_ref
                or evidence.run_id in runs
                or evidence.result_ref in results
                or evidence.trial_id in trials
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "删除轨迹失配或复用了旧试验/Run/结果")
        runs.add(evidence.run_id)
        results.add(evidence.result_ref)
        trials.add(evidence.trial_id)
        halted = (
            bool(evidence.safety_violations)
            or evidence.usage is None
            or evidence.category
            in (
                "unknown",
                "cancelled",
                "budget",
                "safety",
            )
        )
        if evidence.verdict == "passed" and not halted:
            current, tested = step.after, {}
        else:
            tested[step.removed_patch_ref] = evidence
        prefix = evolve(prefix, steps=(*prefix.steps, step))
    return current, tested, halted


def next_deletion(state: ReductionState) -> DeletionRequest | None:
    current, tested, halted = _replay(state)
    if halted or len(state.steps) >= state.max_trials:
        return None
    return _request(current, tested, state.sample)


def record_deletion(state: ReductionState, evidence: ValidationTrialEvidence) -> ReductionState:
    request = next_deletion(state)
    if request is None:
        raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "缩减已经结束或冻结额度不足，不能追加试验")
    step = DeletionStep(**request.model_dump(), evidence=evidence)
    updated = evolve(state, steps=(*state.steps, step))
    _replay(updated)
    return updated


def summarize_reduction(state: ReductionState) -> ReductionSummary:
    current, tested, halted = _replay(state)
    remaining = tuple(atom.patch_ref for atom in current.atoms if atom.patch_ref not in tested)
    indeterminate = tuple(
        ref
        for ref, trial in tested.items()
        if trial.verdict != "failed" or trial.category != "oracle" or trial.usage is None
    )
    violations = tuple(v for step in state.steps for v in step.evidence.safety_violations)
    last = state.steps[-1].evidence if state.steps else None
    if violations:
        status, reason = "needs_review", "safety_violation"
    elif last is not None and last.category == "cancelled":
        status, reason = "cancelled", "operator_cancelled"
    elif last is not None and last.category == "budget":
        status, reason = "budget_exhausted", "trial_budget"
    elif halted:
        status, reason = "needs_review", "unresolved_trial"
    elif not remaining and indeterminate:
        status, reason = "needs_review", "indeterminate_deletions"
    elif remaining:
        status, reason = (
            ("budget_exhausted", "phase_trials")
            if len(state.steps) >= state.max_trials
            else (
                "running",
                "eligible_deletions_remain",
            )
        )
    else:
        status, reason = "complete", "declared_deletions_complete"
    if status != "complete":
        minimality = "not_proven"
    elif not current.atoms:
        minimality = "empty_candidate"
    elif current.model_scope == "deterministic_fixture":
        minimality = "one_minimal_under_fixture"
    else:
        minimality = "empirical"
    return ReductionSummary.model_validate(
        dict(
            state_ref=validation_ref(state),
            final_candidate=current,
            trials=len(state.steps),
            removed_atoms=len(state.initial.atoms) - len(current.atoms),
            tested_final_deletions=tuple(tested),
            untested_final_deletions=remaining,
            indeterminate_final_deletions=indeterminate,
            status=status,
            reason=reason,
            minimality=minimality,
            safety_violations=violations,
        )
    )


def lock_reduced_candidate(state: ReductionState, *, selection_event_ref: str) -> FinalRepairLock:
    summary = summarize_reduction(state)
    if summary.status == "running":
        raise AgentGitError(ErrorCode.VALIDATION, "还有可执行删除测试，尚未锁定最终候选")
    if summary.safety_violations:
        raise AgentGitError(ErrorCode.FORBIDDEN, "存在安全违规，不能自动进入确认")
    if any(s.evidence.usage is None or s.evidence.category == "unknown" for s in state.steps):
        raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "未知计量/效果必须先核对，不能打开确认样本")
    if any(s.evidence.category == "cancelled" for s in state.steps):
        raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "已取消缩减，不能自动进入确认")
    return FinalRepairLock(
        candidate=summary.final_candidate,
        selection_event_ref=selection_event_ref,
        selection_result_refs=(
            *state.screening_result_refs,
            *(s.evidence.result_ref for s in state.steps),
        ),
        prior_run_ids=(*state.screening_run_ids, *(s.evidence.run_id for s in state.steps)),
    )
