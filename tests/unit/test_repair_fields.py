"""字段协议的非单调、预算和来源向量；纯证据不冒充实际执行。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.interventions import (
    InterventionPrecondition,
    NewIntervention,
    PatchProvenance,
    PlanPatch,
    PromptPatch,
    ToolResponsePatch,
)
from agentgit.core.models import CallOccurrence, parse_contract
from agentgit.core.repair import RepairPatch, RepairTrialUsage
from agentgit.core.repair_fields import RepairFieldPlan, RepairFieldState, RepairFieldVariant
from agentgit.core.repair_validation import (
    RepairAtom,
    RepairCandidate,
    ValidationSample,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import AgentConfig, Plan, PlanNode, evolve
from agentgit.repair.field_reduction import (
    lock_field_candidate,
    next_field_trial,
    record_field_trial,
    replay_fields,
    summarize_fields,
)
from agentgit.repair.field_transforms import field_variants
from agentgit.repair.space import patch_ref


def setup_state(*, maximum=12, scope="deterministic_fixture", prompt="原提示\n必要；可删。"):
    config = AgentConfig(prompt="原提示")
    source = Plan(nodes=(PlanNode(key="report", kind="write_report"),))
    precondition = InterventionPrecondition(
        state_ref="a" * 64,
        config_ref="b" * 64,
        validator_ref="c" * 64,
        occurrence=CallOccurrence(
            logical_step_id="model",
            call_index=0,
            attempt=1,
            provider="llm:mock-research/v1",
            signature_digest="d" * 64,
            request_digest="e" * 64,
        ),
    )
    run = str(uuid4())

    def patch(payload, template):
        return RepairPatch(
            template=template,
            intervention=NewIntervention(
                source_run_id=run,
                base_commit="f" * 64,
                target_commit_id="f" * 64,
                target_event_id=1,
                precondition=precondition,
                operator=payload.kind,
                payload=payload,
                effect_policy="test_only"
                if payload.kind == "TOOL_RESPONSE_PATCH"
                else "sandbox_or_approval",
                provenance=PatchProvenance(rationale="固定纯协议向量"),
            ),
        )

    patches = (
        patch(PromptPatch(prompt=prompt), "prompt-verified-sources/v1"),
        patch(
            PlanPatch(
                plan=Plan(
                    nodes=(
                        *source.nodes,
                        PlanNode(key="sources", kind="write_sources", dependencies=("report",)),
                    )
                )
            ),
            "plan-required-artifacts/v1",
        ),
    )
    candidate = RepairCandidate(
        binding_ref="1" * 64,
        source_proposal_ref="2" * 64,
        task_ref="3" * 64,
        atoms=tuple(
            RepairAtom(patch_ref=patch_ref(p), operator=p.intervention.operator) for p in patches
        ),
        model_scope=scope,
    )
    plan = RepairFieldPlan(
        parent_reduction_ref="4" * 64,
        initial=candidate,
        source_config=config,
        source_plan=source,
        completed=(),
        patches=patches,
        sample=ValidationSample(
            role="reduce",
            task_ref=candidate.task_ref,
            response_set_ref="5" * 64,
            validator_ref="c" * 64,
        ),
        prior_run_ids=(run,),
        prior_result_refs=("6" * 64,),
        initial_atomic_minimality="one_minimal_under_fixture"
        if scope == "deterministic_fixture"
        else "empirical",
        max_trials=maximum,
    )
    return RepairFieldState(plan=plan), patch


def result(state, verdict="failed", category="oracle", **changes):
    request = next_field_trial(state)
    assert request is not None
    body = dict(
        phase="reduce",
        protocol_ref=validation_ref(state),
        trial_id=str(uuid4()),
        run_id=str(uuid4()),
        result_ref=uuid4().hex * 2,
        candidate_ref=validation_ref(request.after),
        sample_ref=request.sample_ref,
        validator_ref=request.validator_ref,
        verdict=verdict,
        category=category,
        usage=RepairTrialUsage(
            model_calls=1,
            tool_calls=0,
            tokens=1,
            cost_micro_usd=0,
            unit="mock_utf8_bytes",
            cost_basis="local_mock_free",
        ),
    )
    body.update(changes)
    return ValidationTrialEvidence.model_validate(body)


def test_suffix_first_transform_preserves_source_and_every_other_intervention_field():
    state, _ = setup_state()
    replay = replay_fields(state)
    variants = field_variants(state.plan, replay.candidate, replay.patches, replay.roots).variants
    first = variants[0]
    assert (
        first.operation == "prompt_clause"
        and first.patch.intervention.payload.prompt == "原提示\n必要；"
    )
    before = state.plan.patches[0].intervention
    assert evolve(first.patch.intervention, payload=before.payload) == before
    assert (
        first.after_bytes < first.before_bytes
        and first.parent_patch_ref == first.root_patch_ref == patch_ref(state.plan.patches[0])
    )
    assert any(v.operation == "plan_leaf" for v in variants)
    assert any(v.operation == "plan_dependency" for v in variants)


def test_field_success_invalidates_old_atom_proof_and_rechecks_before_more_fields():
    state, _ = setup_state()
    state = record_field_trial(state, result(state, "passed"))
    summary = summarize_fields(state)
    assert summary.atomic_minimality == "not_proven" and len(summary.pending_atom_refs) == 2
    first = next_field_trial(state)
    assert first.kind == "atomic_recheck"
    state = record_field_trial(state, result(state))
    assert next_field_trial(state).kind == "atomic_recheck"
    state = record_field_trial(state, result(state))
    assert next_field_trial(state).kind == "field"
    assert summarize_fields(state).atomic_minimality == "one_minimal_under_fixture"
    assert first.after.atoms == (state.plan.initial.atoms[1],)


def test_successful_recheck_deletion_restarts_remaining_atoms_non_monotonically():
    state, _ = setup_state()
    state = record_field_trial(state, result(state, "passed"))
    a = next_field_trial(state).changed_patch_ref
    state = record_field_trial(state, result(state))
    state = record_field_trial(state, result(state, "passed"))
    retry = next_field_trial(state)
    assert retry.kind == "atomic_recheck" and retry.changed_patch_ref == a and not retry.after.atoms
    assert summarize_fields(state).atomic_minimality == "not_proven"
    state = record_field_trial(state, result(state))
    assert summarize_fields(state).atomic_minimality == "one_minimal_under_fixture"
    assert summarize_fields(state).removed_atoms == 1


def test_budget_after_changed_field_preserves_passed_candidate_without_old_minimality():
    state, _ = setup_state(maximum=1)
    accepted = next_field_trial(state).after
    state = record_field_trial(state, result(state, "passed"))
    summary = summarize_fields(state)
    assert (
        summary.status == "budget_exhausted"
        and summary.atomic_minimality == summary.field_minimality == "not_proven"
    )
    assert next_field_trial(state) is None
    assert lock_field_candidate(state, selection_event_ref="7" * 64).candidate == accepted


@pytest.mark.parametrize("category", ["unknown", "execution", "budget", "cancelled", "safety"])
def test_incomplete_or_unsafe_outcomes_stop_without_a_field_minimality_claim(category):
    state, _ = setup_state()
    changes = {"usage": None} if category == "unknown" else {}
    if category == "safety":
        changes["safety_violations"] = ("forbidden",)
    state = record_field_trial(state, result(state, "incomplete", category, **changes))
    assert next_field_trial(state) is None
    assert summarize_fields(state).field_minimality == "not_proven"
    if category == "budget":
        assert (
            lock_field_candidate(state, selection_event_ref="7" * 64).candidate
            == state.plan.initial
        )
    else:
        with pytest.raises(AgentGitError):
            lock_field_candidate(state, selection_event_ref="7" * 64)


def test_old_candidate_evidence_cannot_be_reused_after_a_field_change():
    state, _ = setup_state()
    old = result(state, "passed")
    state = record_field_trial(state, old)
    with pytest.raises(AgentGitError):
        record_field_trial(state, old)
    with pytest.raises(AgentGitError):
        record_field_trial(state, result(state, run_id=state.plan.prior_run_ids[0]))


@pytest.mark.parametrize("scope", ["deterministic_fixture", "stochastic_fixture", "provider"])
def test_complete_failed_transformations_only_prove_the_declared_scope(scope):
    state, _ = setup_state(scope=scope)
    while next_field_trial(state) is not None:
        state = record_field_trial(state, result(state))
    summary = summarize_fields(state)
    assert summary.status == "complete" and summary.accepted_fields == 0
    assert summary.field_minimality == (
        "one_step_minimal_under_fixture" if scope == "deterministic_fixture" else "empirical"
    )
    assert not summary.global_optimum
    assert parse_contract(encode(state.model_dump(mode="json")), RepairFieldState) == state


def test_no_prefix_prompt_and_observation_are_explicitly_inapplicable():
    state, make = setup_state(prompt="完整替换原 Prompt")
    observation = make(
        ToolResponsePatch(replacement_call_id=str(uuid4())), "recorded-observation/v1"
    )
    plan = evolve(
        state.plan,
        patches=(state.plan.patches[0], observation),
        initial=evolve(
            state.plan.initial,
            atoms=(
                state.plan.initial.atoms[0],
                RepairAtom(patch_ref=patch_ref(observation), operator="TOOL_RESPONSE_PATCH"),
            ),
        ),
    )
    summary = summarize_fields(RepairFieldState(plan=plan))
    assert summary.status == "complete" and summary.field_minimality == "no_applicable_fields"
    assert set(summary.excluded_reasons) == {
        "prompt:not_appended_text",
        "observation:field_change_not_allowed",
    }


def test_completed_source_nodes_cannot_be_changed_by_plan_field_transformations():
    state, _ = setup_state()
    original = state.plan.source_plan
    changed = evolve(
        state.plan.patches[1],
        intervention=evolve(
            state.plan.patches[1].intervention,
            payload=PlanPatch(
                plan=Plan(
                    nodes=(
                        evolve(original.nodes[0], dependencies=("sources",)),
                        PlanNode(key="sources", kind="write_sources"),
                    )
                )
            ),
        ),
    )
    plan = evolve(
        state.plan,
        completed=("report",),
        patches=(changed,),
        initial=evolve(
            state.plan.initial,
            atoms=(RepairAtom(patch_ref=patch_ref(changed), operator="PLAN_PATCH"),),
        ),
    )
    summary = summarize_fields(RepairFieldState(plan=plan))
    assert (
        summary.excluded_reasons == ("plan:source_nodes_not_preserved",)
        and summary.field_trials == 0
    )


def test_bounded_generation_reports_excluded_clauses_instead_of_claiming_unbounded_search():
    state, _ = setup_state(prompt="原提示\n" + "".join(f"{index}句；" for index in range(130)))
    replay = replay_fields(state)
    generated = field_variants(state.plan, replay.candidate, replay.patches, replay.roots)
    assert len(generated.variants) == 128 and generated.excluded_count == 4
    assert len(generated.reasons) == 4


def test_identical_clause_removals_are_deduplicated_by_effect():
    state, _ = setup_state(prompt="原提示\n" + "句；" * 130)
    replay = replay_fields(state)
    generated = field_variants(state.plan, replay.candidate, replay.patches, replay.roots)
    assert len(generated.variants) == 3 and generated.excluded_count == 0


def test_initial_minimality_cannot_mislabel_provider_or_empty_candidate():
    state, _ = setup_state()
    with pytest.raises(ValidationError):
        evolve(state.plan, initial=evolve(state.plan.initial, model_scope="provider"))
    with pytest.raises(ValidationError):
        evolve(state.plan, initial=evolve(state.plan.initial, atoms=()))
    empty = evolve(
        state.plan,
        initial=evolve(state.plan.initial, atoms=()),
        initial_atomic_minimality="empty_candidate",
    )
    assert summarize_fields(RepairFieldState(plan=empty)).atomic_minimality == "empty_candidate"


def test_variants_and_plan_reject_expansion_wrong_operator_and_duplicate_sources():
    state, _ = setup_state()
    variant = next_field_trial(state).variant
    with pytest.raises(ValidationError):
        evolve(variant, after_bytes=variant.before_bytes)
    with pytest.raises(ValidationError):
        evolve(variant, operation="plan_leaf")
    with pytest.raises(ValidationError):
        evolve(state.plan, prior_run_ids=state.plan.prior_run_ids * 2)
    raw = variant.model_dump(mode="json")
    raw["policy"] = "free-text/v1"
    with pytest.raises(AgentGitError):
        parse_contract(encode(raw), RepairFieldVariant)
    with pytest.raises(AgentGitError):
        replay_fields(evolve(state, plan=evolve(state.plan, patches=())))
