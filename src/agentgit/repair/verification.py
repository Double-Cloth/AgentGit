"""只判定已核验的独立证据，不读取响应池、不执行模型/工具或批准配置。"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_validation import (
    RepairValidationPlan,
    RepairVerificationSummary,
    ValidationTrialEvidence,
    WilsonInterval,
    validation_ref,
)

Z_95 = Decimal("1.95996398454005423552")


def wilson_95(successes: int, samples: int) -> WilsonInterval | None:
    if (
        type(successes) is not int
        or type(samples) is not int
        or not 0 <= successes <= samples <= 9_007_199_254_740_991
    ):
        raise AgentGitError(ErrorCode.VALIDATION, "Wilson 区间需要合法的整数成功数/样本数")
    if samples == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        n, p = Decimal(samples), Decimal(successes) / samples
        denominator = 1 + Z_95 * Z_95 / n
        center = (p + Z_95 * Z_95 / (2 * n)) / denominator
        half = Z_95 * (p * (1 - p) / n + Z_95 * Z_95 / (4 * n * n)).sqrt() / denominator
        lower, upper = max(Decimal(0), center - half), min(Decimal(1), center + half)
        quantum = Decimal("0.000000000000000001")
        return WilsonInterval(
            successes=successes,
            samples=samples,
            lower=format(lower.quantize(quantum, rounding=ROUND_FLOOR), "f"),
            upper=format(upper.quantize(quantum, rounding=ROUND_CEILING), "f"),
        )


def verify_repair(
    plan: RepairValidationPlan,
    evidence: tuple[ValidationTrialEvidence, ...],
) -> RepairVerificationSummary:
    lock = plan.lock
    lock_ref = validation_ref(lock)
    samples = {validation_ref(s): s for s in (*plan.pool.confirm, *plan.pool.regression)}
    seen_samples, trials = set(), set()
    runs, results = set(lock.prior_run_ids), set(lock.selection_result_refs)
    for trial in evidence:
        sample = samples.get(trial.sample_ref)
        if (
            sample is None
            or trial.protocol_ref != validation_ref(plan)
            or trial.phase != sample.role
            or trial.candidate_ref != lock_ref
            or trial.validator_ref != sample.validator_ref
            or trial.phase == "reduce"
            or trial.sample_ref in seen_samples
            or trial.trial_id in trials
            or trial.run_id in runs
            or trial.result_ref in results
        ):
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "确认/回归证据不属于锁定候选或复用了旧身份"
            )
        seen_samples.add(trial.sample_ref)
        trials.add(trial.trial_id)
        runs.add(trial.run_id)
        results.add(trial.result_ref)
    # 按冻结样本顺序归档，输入证据到达顺序不改变报告身份。
    by_sample = {trial.sample_ref: trial for trial in evidence}
    evidence = tuple(by_sample[ref] for ref in samples if ref in by_sample)
    confirm = [e for e in evidence if e.phase == "confirm"]
    regression = [e for e in evidence if e.phase == "regression"]
    determined = sum(e.category == "oracle" for e in confirm)
    passed = sum(e.verdict == "passed" for e in confirm)
    regression_passed = sum(e.verdict == "passed" for e in regression)
    reasons = []
    if not lock.candidate.atoms:
        reasons.append("empty_candidate_no_repair")
    unsafe = bool(lock.prior_safety_violations) or any(e.safety_violations for e in evidence)
    failed_confirmation = determined == 6 and passed < 5
    failed_regression = any(e.verdict == "failed" for e in regression)
    if unsafe:
        reasons.append("safety_violation")
    if failed_confirmation:
        reasons.append("confirmation_threshold_failed")
    if failed_regression:
        reasons.append("regression_failed")
    for name, values, available, required in (
        ("confirm", confirm, len(plan.pool.confirm), 6),
        ("regression", regression, len(plan.pool.regression), 4),
    ):
        if available < required:
            reasons.append(f"missing_{name}_samples")
        if len(values) < available:
            reasons.append(f"missing_{name}_trials")
        if any(e.category != "oracle" for e in values):
            reasons.append(f"incomplete_{name}_evidence")
    if any(e.usage is None for e in evidence):
        reasons.append("unknown_usage")
    if unsafe or failed_confirmation or failed_regression:
        status = "rejected"
    elif reasons:
        status = "needs_review"
    elif lock.candidate.model_scope == "provider":
        status = "empirically_confirmed"
    else:
        status = "verified_under_fixture"
    observed = any(atom.operator == "TOOL_RESPONSE_PATCH" for atom in lock.candidate.atoms)
    successful = status in ("verified_under_fixture", "empirically_confirmed")
    known = [trial.usage for trial in evidence if trial.usage is not None]
    return RepairVerificationSummary.model_validate(
        dict(
            plan_ref=validation_ref(plan),
            lock_ref=lock_ref,
            result_refs=tuple(trial.result_ref for trial in evidence),
            status=status,
            confirm_available=len(plan.pool.confirm),
            confirm_attempted=len(confirm),
            confirm_determinate=determined,
            confirm_passed=passed,
            confirmation_interval=wilson_95(passed, len(confirm)),
            regression_available=len(plan.pool.regression),
            regression_attempted=len(regression),
            regression_passed=regression_passed,
            reasons=tuple(reasons),
            unknown_usage_trials=sum(trial.usage is None for trial in evidence),
            known_model_calls=sum(item.model_calls for item in known),
            known_tool_calls=sum(item.tool_calls for item in known),
            known_mock_utf8_bytes=sum(
                item.tokens for item in known if item.unit == "mock_utf8_bytes"
            ),
            known_provider_tokens=sum(
                item.tokens for item in known if item.unit == "provider_tokens"
            ),
            known_mixed_units=sum(item.tokens for item in known if item.unit == "mixed_units"),
            known_cost_micro_usd=sum(item.cost_micro_usd for item in known),
            cost_bases=tuple(sorted({item.cost_basis for item in known})),
            has_observation_patch=observed,
            production_eligible=successful and not observed,
            limitations=(
                "仅是冻结样本下的经验门槛；小样本及 Wilson 区间不证明线上稳定。",
                "尚未批准或创建正式配置；观测替换仅适用于实验。",
                "原子删除、字段缩减与全局最优是不同结论，本判定不提供最小性证明。",
            ),
        )
    )
