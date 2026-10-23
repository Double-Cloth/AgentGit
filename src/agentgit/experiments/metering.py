"""从真实调用的耐久结果计量；不把检查点滞后、未知结果或估价变成账单。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.model_usage import OPENAI_PRICEBOOK, ModelReply
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import SavedCallOutcome


@dataclass(frozen=True)
class ModelObservation:
    intent: CallIntent
    input_bytes: int
    outcome: SavedCallOutcome | None
    payload: bytes | None
    status: str


def model_usage(records: Sequence[ModelObservation]) -> dict[str, Any]:
    mock = {"calls": 0, "input_bytes": 0, "known_output_bytes": 0, "unknown_calls": 0}
    provider = dict.fromkeys(
        (
            "calls",
            "measured_calls",
            "estimated_usage_calls",
            "not_generated_calls",
            "missing_meter_calls",
            "known_input_tokens",
            "known_output_tokens",
            "known_accounted_tokens",
            "known_cached_tokens",
            "missing_cache_detail_calls",
            "known_reasoning_tokens",
            "missing_reasoning_detail_calls",
            "known_estimated_cost_micro_usd",
            "reserved_cost_micro_usd",
            "unpriced_calls",
        ),
        0,
    )
    pending = unknown = 0
    for record in records:
        pending += record.status == "started"
        unknown += record.status == "outcome_unknown"
        intent, saved = record.intent, record.outcome
        if intent.occurrence.provider == "llm:mock-research/v1":
            if intent.model_budget is not None:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Mock 调用不应包含供应商预算")
            mock["calls"] += 1
            mock["input_bytes"] += record.input_bytes
            if saved is None:
                mock["unknown_calls"] += 1
            else:
                mock["known_output_bytes"] += saved.response_bytes
            continue
        if (
            intent.occurrence.provider != "llm:openai/gpt-5.4-mini-2026-03-17"
            or intent.model_budget is None
            or intent.model_budget.pricebook != OPENAI_PRICEBOOK
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "物理模型计量不支持该供应商或价格表")
        provider["calls"] += 1
        reply = None
        if saved is not None and saved.complete and saved.error is None and record.payload:
            try:
                reply = parse_contract(record.payload, ModelReply)
            except AgentGitError as error:
                if error.code not in (ErrorCode.VALIDATION, ErrorCode.UNSUPPORTED_SCHEMA):
                    raise
        if reply is None:
            # 与 Runtime 相同的已知前置拒绝没有生成；其余无计量响应保持未知及预留。
            if (
                saved is not None
                and saved.error is not None
                and saved.error["code"]
                in (
                    "configuration",
                    "budget_exhausted",
                    "context_limit",
                    "forbidden",
                    "unauthorized",
                )
            ):
                provider["not_generated_calls"] += 1
            else:
                provider["missing_meter_calls"] += 1
                provider["unpriced_calls"] += 1
                provider["reserved_cost_micro_usd"] += intent.model_budget.cost_micro_usd
            continue
        meter = reply.meter
        provider["measured_calls"] += meter.basis == "provider_usage"
        provider["estimated_usage_calls"] += meter.basis == "conservative_bound"
        provider["not_generated_calls"] += meter.basis == "not_generated"
        provider["known_input_tokens"] += meter.input_tokens or 0
        provider["known_output_tokens"] += meter.output_tokens or 0
        provider["known_accounted_tokens"] += (
            meter.accounted_input_tokens + meter.accounted_output_tokens
        )
        provider["known_cached_tokens"] += meter.cached_tokens or 0
        provider["known_reasoning_tokens"] += meter.reasoning_tokens or 0
        provider["missing_cache_detail_calls"] += (
            meter.basis == "provider_usage" and meter.cached_tokens is None
        )
        provider["missing_reasoning_detail_calls"] += (
            meter.basis == "provider_usage" and meter.reasoning_tokens is None
        )
        if meter.price_verified:
            provider["known_estimated_cost_micro_usd"] += meter.estimated_cost_micro_usd
        else:
            provider["unpriced_calls"] += 1
            provider["reserved_cost_micro_usd"] += intent.model_budget.cost_micro_usd
    mock_result: dict[str, Any] = {
        **mock,
        "output_bytes": None if mock["unknown_calls"] else mock["known_output_bytes"],
        "total_bytes": None
        if mock["unknown_calls"]
        else mock["input_bytes"] + mock["known_output_bytes"],
        "cost_basis": "local_mock_free",
        "cost_micro_usd": 0,
    }
    missing_usage = provider["missing_meter_calls"] + provider["estimated_usage_calls"]
    missing_meter = provider["missing_meter_calls"]
    return {
        "schema_version": 1,
        "basis": "original_call_ledger/v1",
        "model_calls": len(records),
        "pending_calls": pending,
        "unknown_calls": unknown,
        "mock": mock_result,
        "provider": {
            **provider,
            "currency": "USD",
            "pricebook": OPENAI_PRICEBOOK,
            "cost_basis": "pricebook_estimate",
            "input_tokens": None if missing_usage else provider["known_input_tokens"],
            "output_tokens": None if missing_usage else provider["known_output_tokens"],
            "accounted_tokens": None if missing_meter else provider["known_accounted_tokens"],
            "cached_tokens": None
            if missing_usage or provider["missing_cache_detail_calls"]
            else provider["known_cached_tokens"],
            "reasoning_tokens": None
            if missing_usage or provider["missing_reasoning_detail_calls"]
            else provider["known_reasoning_tokens"],
            "estimated_cost_micro_usd": None
            if provider["unpriced_calls"]
            else provider["known_estimated_cost_micro_usd"],
            "billed_cost_micro_usd": None if provider["calls"] else 0,
        },
    }
