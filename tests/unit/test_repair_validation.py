"""S10 纯协议向量：合成证据只用于测试内核，不冒充真实 Job/Run 验收。"""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair import RepairTrialUsage
from agentgit.core.repair_validation import (
    FinalRepairLock,
    ReductionState,
    RepairAtom,
    RepairCandidate,
    RepairSamplePool,
    RepairValidationPlan,
    ValidationSample,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import evolve
from agentgit.repair.reduction import (
    lock_reduced_candidate,
    next_deletion,
    record_deletion,
    summarize_reduction,
)
from agentgit.repair.verification import verify_repair, wilson_95


def digest(number):
    return f"{number:064x}"


def candidate(size=3, *, scope="deterministic_fixture", observation=False):
    return RepairCandidate(
        binding_ref=digest(9000),
        source_proposal_ref=digest(9001),
        task_ref=digest(9002),
        atoms=tuple(
            RepairAtom(
                patch_ref=digest(index + 1),
                operator="TOOL_RESPONSE_PATCH"
                if observation and index == size - 1
                else "PROMPT_PATCH"
                if index == 0
                else "PLAN_PATCH"
                if index == 1
                else "TOOL_RESPONSE_PATCH",
            )
            for index in range(size)
        ),
        model_scope=scope,
    )


def usage():
    return RepairTrialUsage(
        model_calls=1,
        tool_calls=0,
        tokens=10,
        cost_micro_usd=0,
        unit="mock_utf8_bytes",
        cost_basis="local_mock_free",
    )


def state(size=3, *, scope="deterministic_fixture", budget=6):
    initial = candidate(size, scope=scope)
    sample = ValidationSample(
        role="reduce",
        task_ref=initial.task_ref,
        response_set_ref=digest(100),
        validator_ref=digest(101),
    )
    return ReductionState(
        initial=initial,
        sample=sample,
        screening_run_ids=(str(uuid4()),),
        screening_result_refs=(digest(102),),
        max_trials=budget,
    )


def outcome(
    phase, candidate_ref, sample, *, protocol_ref, verdict="passed", category="oracle", **changes
):
    body = dict(
        phase=phase,
        protocol_ref=protocol_ref,
        trial_id=str(uuid4()),
        run_id=str(uuid4()),
        result_ref=uuid4().hex * 2,
        candidate_ref=candidate_ref,
        sample_ref=validation_ref(sample),
        validator_ref=sample.validator_ref,
        verdict=verdict,
        category=category,
        usage=usage(),
    )
    body.update(changes)
    return ValidationTrialEvidence.model_validate(body)


def delete_result(current, *, passed=False, **changes):
    request = next_deletion(current)
    assert request is not None
    return outcome(
        "reduce",
        validation_ref(request.after),
        current.sample,
        protocol_ref=validation_ref(current),
        verdict="passed" if passed else "failed",
        **changes,
    )


def plan(*, scope="deterministic_fixture", observation=False, confirms=6, regressions=4):
    selected = candidate(2, scope=scope, observation=observation)
    lock = FinalRepairLock(
        candidate=selected,
        selection_event_ref=digest(200),
        selection_result_refs=(digest(201),),
        prior_run_ids=(str(uuid4()),),
    )
    confirm = tuple(
        ValidationSample(
            role="confirm",
            task_ref=selected.task_ref,
            response_set_ref=digest(300 + i),
            validator_ref=digest(400 + i),
        )
        for i in range(confirms)
    )
    regression = tuple(
        ValidationSample(
            role="regression",
            task_ref=digest(500 + i),
            response_set_ref=digest(600 + i),
            validator_ref=digest(700 + i),
        )
        for i in range(regressions)
    )
    pool = RepairSamplePool(
        screen_reduce_response_refs=(digest(100),), confirm=confirm, regression=regression
    )
    return RepairValidationPlan(
        lock=lock,
        pool=pool,
        opening_event_ref=digest(202),
        opened_for_lock_ref=validation_ref(lock),
    )


def observations(protocol, *, confirms_passed=6, regressions_passed=4):
    return tuple(
        outcome(
            sample.role,
            validation_ref(protocol.lock),
            sample,
            protocol_ref=validation_ref(protocol),
            verdict="passed" if index < bound else "failed",
        )
        for samples, bound in (
            (protocol.pool.confirm, confirms_passed),
            (protocol.pool.regression, regressions_passed),
        )
        for index, sample in enumerate(samples)
    )


def test_redundant_c_is_removed_and_remaining_a_b_are_independently_retested():
    current = state()
    initial_ref = validation_ref(current)
    for passed in (False, False, True, False, False):
        current = record_deletion(current, delete_result(current, passed=passed))
    summary = summarize_reduction(current)
    assert initial_ref != summary.state_ref
    assert summary.trials == 5 and summary.removed_atoms == 1
    assert tuple(atom.patch_ref for atom in summary.final_candidate.atoms) == (digest(1), digest(2))
    assert (
        summary.tested_final_deletions == (digest(1), digest(2))
        and not summary.untested_final_deletions
    )
    assert (
        summary.minimality == "one_minimal_under_fixture" and summary.field_reduction == "not_run"
    )
    assert not summary.global_optimum and next_deletion(current) is None
    assert len({step.evidence.run_id for step in current.steps}) == 5
    restored = ReductionState.model_validate_json(encode(current.model_dump(mode="json")))
    assert summarize_reduction(restored) == summary
    locked = lock_reduced_candidate(restored, selection_event_ref=digest(999))
    assert locked.candidate == summary.final_candidate and len(locked.selection_result_refs) == 6


def test_nonmonotonic_deletion_restarts_after_success_and_does_not_reuse_old_failure():
    current = state()
    for passed in (False, False, True, True, False):
        current = record_deletion(current, delete_result(current, passed=passed))
    summary = summarize_reduction(current)
    assert tuple(atom.patch_ref for atom in summary.final_candidate.atoms) == (digest(2),)
    assert [step.removed_patch_ref for step in current.steps] == [
        digest(i) for i in (1, 2, 3, 1, 2)
    ]
    assert current.steps[0].evidence.candidate_ref != current.steps[3].evidence.candidate_ref
    assert summary.minimality == "one_minimal_under_fixture" and summary.trials == 5


def test_partial_deletions_exhaust_budget_without_minimality_and_lock_exact_remaining_candidate():
    current = state(budget=4)
    for passed in (False, False, True, False):
        current = record_deletion(current, delete_result(current, passed=passed))
    summary = summarize_reduction(current)
    assert summary.status == "budget_exhausted" and summary.reason == "phase_trials"
    assert summary.minimality == "not_proven" and summary.untested_final_deletions == (digest(2),)
    assert next_deletion(current) is None
    assert (
        lock_reduced_candidate(current, selection_event_ref=digest(999)).candidate
        == summary.final_candidate
    )
    with pytest.raises(AgentGitError):
        record_deletion(current, current.steps[0].evidence)


@pytest.mark.parametrize("scope", ["provider", "stochastic_fixture"])
def test_random_conditions_only_report_empirical_reduction(scope):
    current = state(1, scope=scope)
    current = record_deletion(current, delete_result(current))
    assert summarize_reduction(current).minimality == "empirical"


@pytest.mark.parametrize(
    "category,status,reason",
    [
        ("budget", "budget_exhausted", "trial_budget"),
        ("cancelled", "cancelled", "operator_cancelled"),
        ("unknown", "needs_review", "unresolved_trial"),
        ("execution", "needs_review", "indeterminate_deletions"),
        ("safety", "needs_review", "safety_violation"),
    ],
)
def test_incomplete_deletions_preserve_stop_reason_and_never_prove_necessity(
    category, status, reason
):
    current = state(1)
    evidence = delete_result(current)
    evidence = evolve(
        evidence,
        verdict="incomplete",
        category=category,
        safety_violations=("forbidden",) if category == "safety" else (),
    )
    current = record_deletion(current, evidence)
    summary = summarize_reduction(current)
    assert (
        summary.status == status and summary.reason == reason and summary.minimality == "not_proven"
    )
    assert next_deletion(current) is None


def test_unknown_usage_cannot_accept_deletion_or_open_confirmation():
    current = state(1)
    current = record_deletion(current, delete_result(current, passed=True, usage=None))
    summary = summarize_reduction(current)
    assert summary.final_candidate == current.initial and summary.reason == "unresolved_trial"
    with pytest.raises(AgentGitError) as error:
        lock_reduced_candidate(current, selection_event_ref=digest(999))
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN


@pytest.mark.parametrize(
    "field", ["run_id", "result_ref", "trial_id", "candidate_ref", "sample_ref", "validator_ref"]
)
def test_reduction_rejects_stale_reused_or_cross_sample_evidence(field):
    current = state(2)
    first = delete_result(current)
    current = record_deletion(current, first)
    second = delete_result(current)
    changed = (
        getattr(first, field) if field in ("run_id", "result_ref", "trial_id") else digest(9999)
    )
    with pytest.raises(AgentGitError) as error:
        record_deletion(current, evolve(second, **{field: changed}))
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_screening_result_or_run_cannot_count_as_new_deletion_trial():
    current = state(1)
    for change in (
        {"run_id": current.screening_run_ids[0]},
        {"result_ref": current.screening_result_refs[0]},
    ):
        with pytest.raises(AgentGitError):
            record_deletion(current, evolve(delete_result(current), **change))
    with pytest.raises(AgentGitError):
        lock_reduced_candidate(current, selection_event_ref=digest(999))


def test_empty_candidate_has_no_vacuous_atomic_minimality_or_production_repair_claim():
    current = state(1)
    current = record_deletion(current, delete_result(current, passed=True))
    assert summarize_reduction(current).minimality == "empty_candidate"
    protocol = plan()
    empty = evolve(protocol.lock, candidate=evolve(protocol.lock.candidate, atoms=()))
    protocol = evolve(protocol, lock=empty, opened_for_lock_ref=validation_ref(empty))
    summary = verify_repair(protocol, observations(protocol))
    assert summary.status == "needs_review" and "empty_candidate_no_repair" in summary.reasons
    assert not summary.production_eligible


def test_six_independent_confirmations_five_passes_and_four_regressions_meet_fixture_policy():
    protocol = plan()
    summary = verify_repair(protocol, observations(protocol, confirms_passed=5))
    assert summary.status == "verified_under_fixture" and summary.confirm_passed == 5
    assert (
        summary.confirm_attempted == summary.confirm_determinate == 6
        and summary.regression_passed == 4
    )
    assert Decimal(summary.confirmation_interval.lower) < Decimal("0.8")
    assert (
        summary.production_eligible
        and summary.adoption == "not_reviewed"
        and not summary.global_optimum
    )
    assert encode(summary.model_dump(mode="json"))


def test_accidental_screening_success_is_not_promoted_when_confirmation_fails():
    protocol = plan()
    summary = verify_repair(protocol, observations(protocol, confirms_passed=4))
    assert summary.status == "rejected" and "confirmation_threshold_failed" in summary.reasons
    assert not summary.production_eligible


@pytest.mark.parametrize("confirms,regressions", [(0, 4), (3, 4), (5, 4), (6, 0), (6, 3)])
def test_missing_samples_never_satisfy_default_policy_even_if_every_available_trial_passes(
    confirms, regressions
):
    protocol = plan(confirms=confirms, regressions=regressions)
    summary = verify_repair(protocol, observations(protocol))
    assert summary.status == "needs_review" and summary.confirm_attempted == confirms
    assert not summary.production_eligible and summary.reasons
    assert (summary.confirmation_interval is None) == (confirms == 0)


def test_missing_trial_is_not_hidden_from_planned_sample_scope():
    protocol = plan()
    observed = observations(protocol)
    summary = verify_repair(protocol, observed[1:])
    assert summary.confirm_attempted == 5 and summary.confirm_available == 6
    assert summary.status == "needs_review" and "missing_confirm_trials" in summary.reasons


@pytest.mark.parametrize("category", ["budget", "unknown", "execution", "cancelled"])
def test_five_passes_and_one_unfinished_confirmation_is_not_verified(category):
    protocol = plan()
    observed = list(observations(protocol))
    observed[0] = evolve(observed[0], verdict="incomplete", category=category)
    summary = verify_repair(protocol, tuple(observed))
    assert (
        summary.status == "needs_review"
        and summary.confirm_passed == 5
        and summary.confirm_determinate == 5
    )
    assert summary.confirmation_interval.samples == 6 and not summary.production_eligible


def test_unknown_usage_and_any_regression_failure_or_safety_violation_remain_visible():
    protocol = plan()
    observed = list(observations(protocol))
    observed[0] = evolve(observed[0], usage=None)
    summary = verify_repair(protocol, tuple(observed))
    assert summary.status == "needs_review" and summary.reasons == ("unknown_usage",)
    assert (
        verify_repair(protocol, observations(protocol, regressions_passed=3)).status == "rejected"
    )
    observed[0] = evolve(
        observed[0],
        verdict="incomplete",
        category="safety",
        safety_violations=("unauthorized_write",),
    )
    summary = verify_repair(protocol, tuple(observed))
    assert summary.status == "rejected" and "safety_violation" in summary.reasons


def test_observation_success_is_fixture_only_and_real_provider_evidence_is_separate():
    protocol = plan(observation=True)
    summary = verify_repair(protocol, observations(protocol))
    assert (
        summary.status == "verified_under_fixture"
        and summary.has_observation_patch
        and not summary.production_eligible
    )
    provider = plan(scope="provider")
    summary = verify_repair(provider, observations(provider))
    assert summary.status == "empirically_confirmed" and summary.test_only


@pytest.mark.parametrize("location", ["screen", "confirm", "regression", "regression_task"])
def test_relabeling_or_repeating_response_content_does_not_create_independent_samples(location):
    protocol = plan()
    body = protocol.pool.model_dump()
    if location == "screen":
        body["screen_reduce_response_refs"] = (protocol.pool.confirm[0].response_set_ref,)
    elif location == "confirm":
        body["confirm"] = (protocol.pool.confirm[0], protocol.pool.confirm[0])
    elif location == "regression":
        body["regression"] = (
            evolve(
                protocol.pool.regression[0],
                response_set_ref=protocol.pool.confirm[0].response_set_ref,
            ),
        )
    else:
        body["regression"] = (
            protocol.pool.regression[0],
            evolve(protocol.pool.regression[1], task_ref=protocol.pool.regression[0].task_ref),
        )
    with pytest.raises(ValidationError):
        RepairSamplePool.model_validate(body)


@pytest.mark.parametrize(
    "field", ["trial_id", "run_id", "result_ref", "sample_ref", "candidate_ref", "validator_ref"]
)
def test_confirmation_cannot_reuse_old_identity_or_change_candidate_or_sample(field):
    protocol = plan()
    observed = list(observations(protocol))
    value = (
        getattr(observed[0], field)
        if field in ("trial_id", "run_id", "result_ref", "sample_ref")
        else digest(9999)
    )
    observed[1] = evolve(observed[1], **{field: value})
    with pytest.raises(AgentGitError) as error:
        verify_repair(protocol, tuple(observed))
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_opened_pool_binding_and_screening_evidence_cannot_be_swapped_to_new_candidate():
    protocol = plan()
    altered = evolve(
        protocol.lock,
        candidate=evolve(protocol.lock.candidate, atoms=protocol.lock.candidate.atoms[:1]),
    )
    with pytest.raises(ValidationError):
        evolve(protocol, lock=altered)
    observed = list(observations(protocol))
    for change in (
        {"run_id": protocol.lock.prior_run_ids[0]},
        {"result_ref": protocol.lock.selection_result_refs[0]},
    ):
        with pytest.raises(AgentGitError):
            verify_repair(protocol, (evolve(observed[0], **change), *observed[1:]))


def test_changing_pool_or_protocol_cannot_recycle_existing_confirmation_results():
    protocol = plan()
    observed = observations(protocol)
    changed = evolve(protocol, opening_event_ref=digest(999))
    with pytest.raises(AgentGitError) as error:
        verify_repair(changed, observed)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
    changed_pool = evolve(
        protocol.pool,
        confirm=(
            *protocol.pool.confirm[:-1],
            evolve(protocol.pool.confirm[-1], response_set_ref=digest(9998)),
        ),
    )
    changed = evolve(protocol, pool=changed_pool)
    with pytest.raises(AgentGitError):
        verify_repair(changed, observed[:5])


def test_same_reduction_candidate_does_not_make_old_protocol_observation_reusable():
    current = state(2)
    first = delete_result(current)
    current = record_deletion(current, first)
    stale = evolve(delete_result(current), protocol_ref=first.protocol_ref)
    with pytest.raises(AgentGitError):
        record_deletion(current, stale)


@pytest.mark.parametrize(
    "change", [{"required_confirm": 5}, {"minimum_confirm_passes": 4}, {"required_regression": 3}]
)
def test_default_confirmation_policy_cannot_be_lowered_after_selection(change):
    with pytest.raises(ValidationError):
        evolve(plan(), **change)


def test_prior_safety_violation_cannot_be_hidden_by_passing_confirmation():
    protocol = plan()
    unsafe_lock = evolve(protocol.lock, prior_safety_violations=("unauthorized_write",))
    protocol = evolve(protocol, lock=unsafe_lock, opened_for_lock_ref=validation_ref(unsafe_lock))
    assert verify_repair(protocol, observations(protocol)).status == "rejected"


def test_all_results_and_known_usage_are_preserved_with_order_independent_identity():
    protocol = plan()
    observed = observations(protocol, confirms_passed=5)
    summary = verify_repair(protocol, observed)
    assert summary == verify_repair(protocol, observed[::-1])
    assert summary.result_refs == tuple(trial.result_ref for trial in observed)
    assert summary.known_model_calls == 10 and summary.known_tool_calls == 0
    assert summary.known_mock_utf8_bytes == 100 and summary.known_provider_tokens == 0
    assert summary.unknown_usage_trials == summary.known_cost_micro_usd == 0
    assert summary.usage_scope == "confirm_regression_only"


def test_partial_metering_is_explicit_and_mock_bytes_are_not_added_to_provider_tokens():
    protocol = plan()
    observed = list(observations(protocol))
    observed[0] = evolve(observed[0], usage=None)
    observed[1] = evolve(
        observed[1],
        usage=evolve(
            usage(),
            tokens=20,
            unit="provider_tokens",
            cost_micro_usd=3,
            cost_basis="pricebook_estimate",
        ),
    )
    summary = verify_repair(protocol, tuple(observed))
    assert summary.status == "needs_review" and summary.unknown_usage_trials == 1
    assert summary.known_mock_utf8_bytes == 80 and summary.known_provider_tokens == 20
    assert summary.known_cost_micro_usd == 3 and summary.known_model_calls == 9
    assert set(summary.cost_bases) == {"local_mock_free", "pricebook_estimate"}


def test_recorded_recovery_proof_cannot_be_relabeled_as_independent_confirmation():
    protocol = plan()
    observed = observations(protocol)[0]
    with pytest.raises(ValidationError):
        evolve(observed, usage=evolve(usage(), cost_basis="recorded_no_external_calls"))


def test_wilson_score_has_real_small_sample_width_and_exact_boundary_behavior():
    assert wilson_95(0, 0) is None
    five = wilson_95(5, 6)
    assert Decimal("0.43") < Decimal(five.lower) < Decimal("0.44")
    assert Decimal("0.969") < Decimal(five.upper) < Decimal("0.971")
    none, all_passed = wilson_95(0, 6), wilson_95(6, 6)
    assert Decimal(none.lower) == 0 and Decimal(all_passed.upper) == 1
    assert Decimal("0.39") < Decimal(none.upper) < Decimal("0.40")
    assert abs(Decimal(none.upper) + Decimal(all_passed.lower) - 1) < Decimal(
        "0.000000000000000003"
    )
    large = wilson_95(50, 60)
    assert Decimal(large.lower) > Decimal(five.lower) and Decimal(large.upper) < Decimal(five.upper)


@pytest.mark.parametrize(
    "successes,samples", [(-1, 6), (7, 6), (0, -1), (True, 6), (1, 6.0), (0, 10**20)]
)
def test_invalid_statistical_inputs_are_rejected(successes, samples):
    with pytest.raises(AgentGitError):
        wilson_95(successes, samples)
