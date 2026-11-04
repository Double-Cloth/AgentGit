"""真实模型的固定配置、整数价格表和可保存计量；不读取密钥或访问网络。"""

from typing import Annotated, Any, Final, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, NonNegative

OPENAI_MODEL: Final = "gpt-5.4-mini-2026-03-17"
OPENAI_PRICEBOOK: Final = "openai-gpt-5.4-mini-standard-usd/2026-10-23"


class OpenAIOptions(Contract):
    model: Literal["gpt-5.4-mini-2026-03-17"] = OPENAI_MODEL
    pricebook: Literal["openai-gpt-5.4-mini-standard-usd/2026-10-23"] = OPENAI_PRICEBOOK
    service_tier: Literal["default"] = "default"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "none"
    max_input_tokens: Annotated[int, Field(ge=1, le=262_144)] = 32_768
    max_output_tokens: Annotated[int, Field(ge=16, le=32_768)] = 8192
    timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 30_000


def price_micro_usd(input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> int:
    """每 Token 的 nano-USD 为 750/75/4500；仅在合计时向上取整到 micro-USD。"""
    if (
        any(
            type(value) is not int or value < 0
            for value in (input_tokens, output_tokens, cached_tokens)
        )
        or cached_tokens > input_tokens
    ):
        raise ValueError("价格计算需要合法的整数 Token 数")
    nano = (input_tokens - cached_tokens) * 750 + cached_tokens * 75 + output_tokens * 4500
    return (nano + 999) // 1000


class ModelMeter(Contract):
    currency: Literal["USD"] = "USD"
    pricebook: Literal["openai-gpt-5.4-mini-standard-usd/2026-10-23"] = OPENAI_PRICEBOOK
    basis: Literal["provider_usage", "conservative_bound", "not_generated"]
    counted_input_tokens: NonNegative | None
    input_tokens: NonNegative | None
    output_tokens: NonNegative | None
    cached_tokens: NonNegative | None
    cache_write_tokens: NonNegative | None
    reasoning_tokens: NonNegative | None
    price_verified: bool
    accounted_input_tokens: NonNegative
    accounted_output_tokens: NonNegative
    estimated_cost_micro_usd: NonNegative

    @model_validator(mode="after")
    def consistent_usage(self) -> "ModelMeter":
        if self.basis == "provider_usage":
            if self.input_tokens is None or self.output_tokens is None:
                raise ValueError("供应商计量不能缺少输入或输出 Token")
            if self.cached_tokens is not None and self.cached_tokens > self.input_tokens:
                raise ValueError("缓存 Token 不能超过总输入")
            if self.reasoning_tokens is not None and self.reasoning_tokens > self.output_tokens:
                raise ValueError("推理 Token 已包含在总输出中")
            if (self.accounted_input_tokens, self.accounted_output_tokens) != (
                self.input_tokens,
                self.output_tokens,
            ):
                raise ValueError("计量与预算结算不符")
        elif any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cached_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
            )
        ):
            raise ValueError("未知 usage 不能填入假计量")
        if self.basis == "not_generated" and (
            self.accounted_input_tokens or self.accounted_output_tokens
        ):
            raise ValueError("未生成响应不能计生成用量")
        if self.cache_write_tokens and self.price_verified:
            raise ValueError("价格表未覆盖单独缓存写入计价")
        expected = price_micro_usd(
            self.accounted_input_tokens, self.accounted_output_tokens, self.cached_tokens or 0
        )
        if self.estimated_cost_micro_usd != expected:
            raise ValueError("价格表估计与 Token 数不符")
        return self


class ModelTotals(Contract):
    currency: Literal["USD"] = "USD"
    pricebook: Literal["openai-gpt-5.4-mini-standard-usd/2026-10-23"] = OPENAI_PRICEBOOK
    measured_calls: NonNegative = 0
    estimated_calls: NonNegative = 0
    not_generated_calls: NonNegative = 0
    measured_input_tokens: NonNegative = 0
    measured_output_tokens: NonNegative = 0
    cached_tokens: NonNegative = 0
    reasoning_tokens: NonNegative = 0
    missing_cache_detail_calls: NonNegative = 0
    missing_reasoning_detail_calls: NonNegative = 0
    accounted_tokens: NonNegative = 0
    reserved_cost_micro_usd: NonNegative = 0
    unpriced_cost_micro_usd: NonNegative = 0
    unpriced_calls: NonNegative = 0
    next_retry_delay_ms: Annotated[int, Field(ge=0, le=120_000)] = 0

    @model_validator(mode="after")
    def retained_price_reservation(self) -> "ModelTotals":
        if self.unpriced_cost_micro_usd > self.reserved_cost_micro_usd:
            raise ValueError("未结算费用不能超过总预留")
        return self


class ModelReply(Contract):
    protocol: Literal["openai-responses-decision/v1"] = "openai-responses-decision/v1"
    model: Literal["gpt-5.4-mini-2026-03-17"] = OPENAI_MODEL
    disposition: Literal["decision", "retry", "stop"]
    decision: dict[str, Any] | None
    error_code: (
        Literal["model_error", "model_empty", "validation", "configuration", "context_limit"] | None
    )
    meter: ModelMeter
    response_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")] | None
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")] | None
    count_request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")] | None
    client_request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    count_client_request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    retry_after_ms: Annotated[int, Field(ge=0, le=120_000)] | None

    @model_validator(mode="after")
    def consistent_result(self) -> "ModelReply":
        if self.disposition == "decision":
            if self.decision is None or self.error_code is not None:
                raise ValueError("成功响应必须且仅包含决策")
            if self.meter.basis != "provider_usage" or not self.meter.price_verified:
                raise ValueError("成功决策必须包含可核验的供应商计量与价格版本")
        elif self.decision is not None or self.error_code is None:
            raise ValueError("错误响应必须且仅包含错误码")
        if self.disposition == "retry" and (
            self.error_code != "model_error" or self.meter.basis != "not_generated"
        ):
            raise ValueError("只允许确定未生成的暂时失败交给运行时重试")
        return self
