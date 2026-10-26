"""字段成功后重做原子删除；重算不可变轨迹，不执行模型或打开确认池。"""

from dataclasses import dataclass

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair import RepairPatch
from agentgit.core.repair_fields import (
    RepairFieldRequest,
    RepairFieldState,
    RepairFieldStep,
    RepairFieldSummary,
)
from agentgit.core.repair_validation import (
    FinalRepairLock,
    RepairCandidate,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import evolve
from agentgit.repair.field_transforms import GeneratedFields, field_variants
from agentgit.repair.space import patch_ref


@dataclass
class FieldReplay:
    candidate: RepairCandidate
    patches: dict[str, RepairPatch]
    roots: dict[str, str]
    tested_fields: set[str]
    tested_atoms: set[str]
    rechecking: bool
    atomic_valid: bool
    halted: bool = False
    accepted_fields: int = 0


def _generated(state: RepairFieldState, replay: FieldReplay) -> GeneratedFields:
    return field_variants(state.plan, replay.candidate, replay.patches, replay.roots)


def _request(state: RepairFieldState, replay: FieldReplay) -> RepairFieldRequest | None:
    if replay.halted:
        return None
    current, sample = replay.candidate, state.plan.sample
    if replay.rechecking:
        atom = next((a for a in current.atoms if a.patch_ref not in replay.tested_atoms), None)
        if atom is not None:
            return RepairFieldRequest(
                kind="atomic_recheck",
                before_ref=validation_ref(current),
                changed_patch_ref=atom.patch_ref,
                after=evolve(current, atoms=tuple(a for a in current.atoms if a != atom)),
                variant=None,
                sample_ref=validation_ref(sample),
                validator_ref=sample.validator_ref,
            )
    for variant in _generated(state, replay).variants:
        if validation_ref(variant) in replay.tested_fields:
            continue
        after = evolve(
            current,
            atoms=tuple(
                evolve(atom, patch_ref=patch_ref(variant.patch))
                if atom.patch_ref == variant.parent_patch_ref
                else atom
                for atom in current.atoms
            ),
        )
        return RepairFieldRequest(
            kind="field",
            before_ref=validation_ref(current),
            changed_patch_ref=variant.parent_patch_ref,
            after=after,
            variant=variant,
            sample_ref=validation_ref(sample),
            validator_ref=sample.validator_ref,
        )
    return None


def replay_fields(state: RepairFieldState) -> FieldReplay:
    patches = {patch_ref(p): p for p in state.plan.patches}
    if len(patches) != len(state.plan.patches) or any(
        a.patch_ref not in patches or patches[a.patch_ref].intervention.operator != a.operator
        for a in state.plan.initial.atoms
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "字段初始候选必须属于确切冻结补丁")
    proven = state.plan.initial_atomic_minimality != "not_proven"
    replay = FieldReplay(
        state.plan.initial, patches, {ref: ref for ref in patches}, set(), set(), not proven, proven
    )
    runs, results, trials = set(state.plan.prior_run_ids), set(state.plan.prior_result_refs), set()
    prefix = evolve(state, steps=())
    for step in state.steps:
        expected, evidence = _request(prefix, replay), step.evidence
        if (
            expected != step.request
            or expected is None
            or evidence.phase != "reduce"
            or evidence.protocol_ref != validation_ref(prefix)
            or evidence.candidate_ref != validation_ref(step.request.after)
            or evidence.sample_ref != step.request.sample_ref
            or evidence.validator_ref != step.request.validator_ref
            or evidence.run_id in runs
            or evidence.result_ref in results
            or evidence.trial_id in trials
        ):
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "字段轨迹或必要性复查失配，或复用了旧身份"
            )
        runs.add(evidence.run_id)
        results.add(evidence.result_ref)
        trials.add(evidence.trial_id)
        replay.halted = (
            bool(evidence.safety_violations)
            or evidence.usage is None
            or evidence.category != "oracle"
        )
        if evidence.verdict == "passed" and not replay.halted:
            variant = step.request.variant
            if variant is not None:
                ref = patch_ref(variant.patch)
                replay.patches[ref], replay.roots[ref] = variant.patch, variant.root_patch_ref
                replay.accepted_fields += 1
            replay.candidate = step.request.after
            replay.tested_atoms, replay.tested_fields = set(), set()
            replay.rechecking, replay.atomic_valid = True, False
            if not replay.candidate.atoms:
                replay.rechecking, replay.atomic_valid = False, True
        elif evidence.verdict == "failed" and not replay.halted:
            if step.request.variant is not None:
                replay.tested_fields.add(validation_ref(step.request.variant))
            else:
                replay.tested_atoms.add(step.request.changed_patch_ref)
                if all(a.patch_ref in replay.tested_atoms for a in replay.candidate.atoms):
                    replay.rechecking, replay.atomic_valid = False, True
        prefix = evolve(prefix, steps=(*prefix.steps, step))
    return replay


def next_field_trial(state: RepairFieldState) -> RepairFieldRequest | None:
    replay = replay_fields(state)
    return _request(state, replay) if len(state.steps) < state.plan.max_trials else None


def record_field_trial(
    state: RepairFieldState, evidence: ValidationTrialEvidence
) -> RepairFieldState:
    request = next_field_trial(state)
    if request is None:
        raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "字段阶段已经停止或冻结额度不足")
    updated = evolve(
        state, steps=(*state.steps, RepairFieldStep(request=request, evidence=evidence))
    )
    replay_fields(updated)
    return updated


def summarize_fields(
    state: RepairFieldState, *, budget_stopped: bool = False
) -> RepairFieldSummary:
    replay = replay_fields(state)
    generated = _generated(state, replay)
    pending_fields = tuple(
        validation_ref(v)
        for v in generated.variants
        if validation_ref(v) not in replay.tested_fields
    )
    pending_atoms = tuple(
        a.patch_ref
        for a in replay.candidate.atoms
        if replay.rechecking and a.patch_ref not in replay.tested_atoms
    )
    last = state.steps[-1].evidence if state.steps else None
    if replay.halted:
        status, reason = (
            ("cancelled", "operator_cancelled")
            if last is not None and last.category == "cancelled"
            else ("budget_exhausted", "trial_budget")
            if last is not None and last.category == "budget"
            else ("needs_review", "incomplete_or_unsafe_trial")
        )
    elif pending_atoms or pending_fields:
        status, reason = (
            ("budget_exhausted", "search_budget")
            if budget_stopped or state.plan.parent_budget_stop is not None
            else ("budget_exhausted", "phase_trials")
            if len(state.steps) >= state.plan.max_trials
            else ("running", "declared_transformations_remain")
        )
    else:
        status, reason = "complete", "declared_transformations_complete"
    atomic = (
        "not_proven"
        if not replay.atomic_valid
        else "empty_candidate"
        if not replay.candidate.atoms
        else "one_minimal_under_fixture"
        if replay.candidate.model_scope == "deterministic_fixture"
        else "empirical"
    )
    field = (
        "not_proven"
        if status != "complete"
        else "no_applicable_fields"
        if not generated.variants
        else "one_step_minimal_under_fixture"
        if replay.candidate.model_scope == "deterministic_fixture"
        else "empirical"
    )
    return RepairFieldSummary.model_validate(
        dict(
            state_ref=validation_ref(state),
            final_candidate=replay.candidate,
            status=status,
            reason=reason,
            field_trials=sum(s.request.kind == "field" for s in state.steps),
            atomic_recheck_trials=sum(s.request.kind == "atomic_recheck" for s in state.steps),
            accepted_fields=replay.accepted_fields,
            removed_atoms=len(state.plan.initial.atoms) - len(replay.candidate.atoms),
            atomic_minimality=atomic,
            field_minimality=field,
            tested_field_refs=tuple(
                validation_ref(v)
                for v in generated.variants
                if validation_ref(v) in replay.tested_fields
            ),
            pending_field_refs=pending_fields,
            pending_atom_refs=pending_atoms,
            excluded_transform_count=generated.excluded_count,
            excluded_reasons=generated.reasons,
        )
    )


def lock_field_candidate(
    state: RepairFieldState, *, selection_event_ref: str, budget_stopped: bool = False
) -> FinalRepairLock:
    summary = summarize_fields(state, budget_stopped=budget_stopped)
    if summary.status not in ("complete", "budget_exhausted") or any(
        step.evidence.category not in ("oracle", "budget")
        or step.evidence.usage is None
        or step.evidence.safety_violations
        for step in state.steps
    ):
        raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "字段未排空、取消或证据不完整，不能锁定")
    return FinalRepairLock(
        candidate=summary.final_candidate,
        selection_event_ref=selection_event_ref,
        selection_result_refs=(
            *state.plan.prior_result_refs,
            *(step.evidence.result_ref for step in state.steps),
        ),
        prior_run_ids=(*state.plan.prior_run_ids, *(step.evidence.run_id for step in state.steps)),
    )
