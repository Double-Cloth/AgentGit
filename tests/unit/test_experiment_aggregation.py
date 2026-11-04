"""统计覆盖范围、精确方差和费用未知的边界，不调用外部服务。"""

from fractions import Fraction

import pytest

from agentgit.core.canonical import MAX_INTEGER, ObjectType, encode, object_digest
from agentgit.core.model_usage import OPENAI_PRICEBOOK, ModelMeter, ModelReply, price_micro_usd
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass, ModelCallBudget
from agentgit.core.research import SavedCallOutcome
from agentgit.experiments.metering import ModelObservation, model_usage
from agentgit.experiments.statistics import describe, experiment_statistics


def observation(
    *, provider=False, payload=b"result", error=None, complete=True, status="succeeded"
):
    budget = (
        ModelCallBudget(tokens=200, cost_micro_usd=1000, pricebook=OPENAI_PRICEBOOK)
        if provider
        else None
    )
    request = object_digest(ObjectType.BLOB, b"input")
    intent = CallIntent(
        occurrence=CallOccurrence(
            logical_step_id="model-1",
            call_index=0,
            attempt=1,
            provider="llm:openai/gpt-5.4-mini-2026-03-17" if provider else "llm:mock-research/v1",
            signature_digest="a" * 64,
            request_digest=request,
        ),
        effect=EffectClass.READ_ONLY,
        request_ref=request,
        checkpoint=None,
        max_response_bytes=1024,
        model_budget=budget,
    )
    saved = (
        SavedCallOutcome(
            response_ref=object_digest(ObjectType.BLOB, payload),
            response_bytes=len(payload),
            complete=complete,
            error={"code": error, "message": "固定测试错误"} if error else None,
        )
        if status not in ("started", "outcome_unknown")
        else None
    )
    return ModelObservation(intent, 5, saved, payload if saved else None, status)


def provider_reply(*, basis="provider_usage", verified=True):
    measured = basis == "provider_usage"
    generated = basis != "not_generated"
    meter = ModelMeter(
        basis=basis,
        counted_input_tokens=10,
        input_tokens=10 if measured else None,
        output_tokens=2 if measured else None,
        cached_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        price_verified=verified,
        accounted_input_tokens=10 if generated else 0,
        accounted_output_tokens=2 if generated else 0,
        estimated_cost_micro_usd=price_micro_usd(10, 2) if generated else 0,
    )
    reply = ModelReply(
        disposition="stop",
        decision=None,
        error_code="model_error",
        meter=meter,
        response_id=None,
        request_id=None,
        count_request_id=None,
        client_request_id="a" * 32,
        count_client_request_id="b" * 32,
        http_status=200,
        retry_after_ms=None,
    )
    return encode(reply.model_dump(mode="json"))


def trial(verdict="passed", *, kind="mock_utf8_bytes", measurement=None, attempt=1):
    return {
        "group": "A",
        "verdict": verdict,
        "status": "completed"
        if verdict == "passed"
        else "failed"
        if verdict == "failed"
        else "cancelled",
        "attempt": attempt,
        "assessment": {
            "evaluation": {"errors": [] if verdict == "passed" else ["missing_artifacts"]}
        },
        "replay": {
            "actual": {
                "model_calls": 1,
                "tool_calls": 0,
                "wall_ms": 10,
                "unknown_outcomes": 0,
                "model_usage": measurement or model_usage([observation()]),
            },
            "new_usage": {
                "model_calls": 1,
                "tool_calls": 0,
                "steps": 1,
                "replans": 0,
                "tokens": 11,
            },
            "logical_usage": {"usage_kind": kind},
        },
    }


def test_exact_fraction_variance_and_overflow_safe_serialization():
    result = describe([MAX_INTEGER, MAX_INTEGER - 2], population=3, unit="count")
    assert result["sum"] == str(2 * MAX_INTEGER - 2)
    assert result["mean"] == {"numerator": str(MAX_INTEGER - 1), "denominator": "1"}
    assert result["sample_variance"] == {"numerator": "2", "denominator": "1"}
    assert result["not_applicable_samples"] == 1
    encode(result)
    for values in ([0, 1, 5], [9, 9, 9], [2, 11, 37, 92]):
        actual = describe(values, population=len(values), unit="count")["sample_variance"]
        mean = Fraction(sum(values), len(values))
        expected = sum((Fraction(value) - mean) ** 2 for value in values) / (len(values) - 1)
        assert Fraction(int(actual["numerator"]), int(actual["denominator"])) == expected


def test_missing_samples_and_single_sample_are_not_zero_variance():
    result = describe([None, 7], population=3, unit="micro_USD")
    assert result["sum"] is None and result["known_sum"] == "7"
    assert result["missing_samples"] == result["not_applicable_samples"] == 1
    assert result["mean"] == {"numerator": "7", "denominator": "1"}
    assert result["sample_variance"] is None
    assert describe([], population=0, unit="count")["mean"] is None


@pytest.mark.parametrize("values", [[True], [-1], [1.2]])
def test_metric_values_cannot_hide_boolean_float_or_negative(values):
    with pytest.raises(ValueError):
        describe(values, population=1, unit="count")


def test_mock_and_provider_are_distinct_and_missing_optional_details_remain_unknown():
    result = model_usage([observation(), observation(provider=True, payload=provider_reply())])
    assert result["mock"]["total_bytes"] == 11 and result["mock"]["cost_basis"] == "local_mock_free"
    provider = result["provider"]
    assert provider["input_tokens"] == 10 and provider["output_tokens"] == 2
    assert provider["estimated_cost_micro_usd"] == 17 and provider["billed_cost_micro_usd"] is None
    assert provider["cached_tokens"] is None and provider["reasoning_tokens"] is None
    assert provider["missing_cache_detail_calls"] == 1


@pytest.mark.parametrize("status", ["started", "outcome_unknown"])
def test_unfinished_provider_retains_reservation_and_missing_usage(status):
    result = model_usage(
        [
            observation(provider=True, payload=provider_reply()),
            observation(provider=True, status=status),
        ]
    )
    provider = result["provider"]
    assert provider["input_tokens"] is None and provider["known_input_tokens"] == 10
    assert provider["estimated_cost_micro_usd"] is None
    assert provider["known_estimated_cost_micro_usd"] == 17
    assert provider["reserved_cost_micro_usd"] == 1000 and provider["unpriced_calls"] == 1
    assert result["pending_calls" if status == "started" else "unknown_calls"] == 1


@pytest.mark.parametrize("payload", [b"bad", b'{"schema_version":2}'])
def test_invalid_or_unsupported_meter_keeps_unknown_cost(payload):
    provider = model_usage([observation(provider=True, payload=payload)])["provider"]
    assert provider["estimated_cost_micro_usd"] is None
    assert provider["reserved_cost_micro_usd"] == 1000


def test_conservative_usage_and_unpriced_receipt_are_not_measured_or_billed():
    conservative = model_usage(
        [observation(provider=True, payload=provider_reply(basis="conservative_bound"))]
    )["provider"]
    assert conservative["input_tokens"] is None and conservative["accounted_tokens"] == 12
    assert (
        conservative["estimated_usage_calls"] == 1
        and conservative["estimated_cost_micro_usd"] == 17
    )
    unpriced = model_usage([observation(provider=True, payload=provider_reply(verified=False))])[
        "provider"
    ]
    assert unpriced["input_tokens"] == 10 and unpriced["estimated_cost_micro_usd"] is None
    assert unpriced["reserved_cost_micro_usd"] == 1000 and unpriced["billed_cost_micro_usd"] is None


def test_known_preflight_and_not_generated_receipts_release_reservation():
    for record in (
        observation(provider=True, error="configuration", payload=b""),
        observation(provider=True, payload=provider_reply(basis="not_generated")),
    ):
        provider = model_usage([record])["provider"]
        assert provider["not_generated_calls"] == 1
        assert provider["estimated_cost_micro_usd"] == provider["reserved_cost_micro_usd"] == 0


def test_partial_trials_stay_in_success_denominator_and_out_of_full_distribution():
    items = [trial(), trial("failed"), trial("incomplete", attempt=2)]
    result = experiment_statistics(items)
    group = result["groups"][0]
    assert group["success_rate"] == {"numerator": "1", "denominator": "3"}
    assert group["assessed_success_rate"] == {"numerator": "1", "denominator": "2"}
    assert group["hard_assertions"]["not_evaluated"] == 1
    assert group["hard_assertions"]["failure_codes"] == {"missing_artifacts": 1}
    assert group["assessed_trials"]["trial_count"] == 2
    assert group["consumption_to_date"]["trial_count"] == 3
    assert group["recovery"]["success_rate"] == {"numerator": "0", "denominator": "1"}
    assert result["groups"][1]["success_rate"] is None
    encode(result)


def test_logical_mixed_units_are_not_added_to_provider_tokens():
    group = experiment_statistics(
        [trial(kind="mixed_units"), trial(kind="provider_tokens"), trial()]
    )["groups"][0]
    metrics = group["assessed_trials"]["metrics"]
    for kind in ("mixed_units", "provider_tokens", "mock_utf8_bytes"):
        assert metrics["checkpoint.tokens." + kind]["samples"] == 1
        assert metrics["checkpoint.tokens." + kind]["not_applicable_samples"] == 2


@pytest.mark.parametrize("verdict", ["incomplete", "failed"])
def test_no_success_is_inferred_from_all_unfinished_or_failed_trials(verdict):
    group = experiment_statistics([trial(verdict), trial(verdict)])["groups"][0]
    assert group["success_rate"] == {"numerator": "0", "denominator": "1"}
    assert group["passed"] == 0
    if verdict == "incomplete":
        assert group["assessed_success_rate"] is None
        assert group["assessed_trials"] == {"trial_count": 0, "metrics": {}}
        assert group["hard_assertions"]["not_evaluated"] == 2


def test_unknown_mock_result_has_unknown_bytes_and_explicit_local_free_cost():
    result = model_usage([observation(status="outcome_unknown")])
    assert result["mock"]["total_bytes"] is None
    assert result["mock"]["known_output_bytes"] == 0
    assert result["mock"]["cost_basis"] == "local_mock_free"
    assert result["provider"]["calls"] == 0


@pytest.mark.parametrize(("error", "complete"), [("output_limit", False), ("model_error", True)])
def test_truncated_or_error_result_does_not_invent_provider_usage(error, complete):
    provider = model_usage(
        [observation(provider=True, payload=b"bad", error=error, complete=complete)]
    )["provider"]
    assert provider["input_tokens"] is None and provider["estimated_cost_micro_usd"] is None
    assert provider["reserved_cost_micro_usd"] == 1000
