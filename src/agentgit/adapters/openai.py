"""单一 OpenAI Responses 快照适配器；一次 invoke 至多一次计数和一次生成。"""

import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol, TypedDict

from agentgit.adapters.model_http import FixedOpenAIHTTPS, HTTPReply, TransportFailure
from agentgit.adapters.model_protocol import (
    model_signature,
    parse_structured_decision,
    response_input,
)
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.model_usage import ModelMeter, ModelReply, OpenAIOptions, price_micro_usd
from agentgit.core.models import parse_contract
from agentgit.core.research import ModelRequest


class HTTPTransport(Protocol):
    def post(
        self,
        path: str,
        body: bytes,
        *,
        api_key: str,
        client_request_id: str,
        deadline: float,
        max_bytes: int,
    ) -> HTTPReply: ...


class _SavedReply(TypedDict):
    http: HTTPReply
    data: dict[str, Any]
    generated: bool
    price_verified: bool


def _integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _identity(value: Any) -> str | None:
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) else None
    )


def _retry_delay(value: str | None) -> int | None:
    if value is None:
        return 1000
    if value.isascii() and value.isdecimal() and len(value) <= 6:
        delay = int(value) * 1000
    else:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return None
            delay = max(0, int((parsed - datetime.now(UTC)).total_seconds() * 1000) + 1)
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if delay <= 120_000 else None


def _json_object(payload: bytes, api_key: str) -> dict[str, Any] | None:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("重复字段")
            result[name] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique)
        # 检查解码后的转义文本，拒绝供应商把本次凭据回显进可保存结果。
        if not isinstance(value, dict) or api_key in json.dumps(value, ensure_ascii=False):
            return None
        return value
    except (ValueError, UnicodeError, RecursionError):
        return None


def _meter(
    raw: Any,
    options: OpenAIOptions,
    counted: int | None,
    *,
    generated: bool,
    price_verified: bool = True,
) -> ModelMeter:
    values: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
    }
    valid = False
    basis: Literal["provider_usage", "conservative_bound", "not_generated"]
    if isinstance(raw, dict):
        values["input_tokens"] = _integer(raw.get("input_tokens"))
        values["output_tokens"] = _integer(raw.get("output_tokens"))
        inputs, outputs = raw.get("input_tokens_details"), raw.get("output_tokens_details")
        if isinstance(inputs, dict):
            values["cached_tokens"] = _integer(inputs.get("cached_tokens"))
            values["cache_write_tokens"] = _integer(inputs.get("cache_write_tokens"))
        if isinstance(outputs, dict):
            values["reasoning_tokens"] = _integer(outputs.get("reasoning_tokens"))
        incoming, outgoing = values["input_tokens"], values["output_tokens"]
        valid = incoming is not None and outgoing is not None
        if isinstance(inputs, dict):
            valid = valid and all(
                field not in inputs or inputs[field] is None or _integer(inputs[field]) is not None
                for field in ("cached_tokens", "cache_write_tokens")
            )
        elif inputs is not None:
            valid = False
        if isinstance(outputs, dict):
            valid = valid and (
                "reasoning_tokens" not in outputs
                or outputs["reasoning_tokens"] is None
                or _integer(outputs["reasoning_tokens"]) is not None
            )
        elif outputs is not None:
            valid = False
        if valid:
            assert incoming is not None and outgoing is not None
            valid = (
                (values["cached_tokens"] or 0) <= incoming
                and (values["reasoning_tokens"] or 0) <= outgoing
                and _integer(raw.get("total_tokens")) == incoming + outgoing
            )
    if valid and generated:
        incoming, outgoing = values["input_tokens"], values["output_tokens"]
        assert incoming is not None and outgoing is not None
        basis = "provider_usage"
        price_verified = price_verified and not bool(values["cache_write_tokens"])
    else:
        values = dict.fromkeys(values)
        incoming = (
            0 if not generated else (counted if counted is not None else options.max_input_tokens)
        )
        outgoing = options.max_output_tokens if generated else 0
        basis = "conservative_bound" if generated else "not_generated"
    return ModelMeter(
        basis=basis,
        counted_input_tokens=counted,
        input_tokens=values["input_tokens"],
        output_tokens=values["output_tokens"],
        cached_tokens=values["cached_tokens"],
        cache_write_tokens=values["cache_write_tokens"],
        reasoning_tokens=values["reasoning_tokens"],
        price_verified=price_verified,
        accounted_input_tokens=incoming,
        accounted_output_tokens=outgoing,
        estimated_cost_micro_usd=price_micro_usd(incoming, outgoing, values["cached_tokens"] or 0),
    )


class OpenAIResearchModel:
    def __init__(
        self,
        options: OpenAIOptions,
        *,
        cost_limit_micro_usd: int,
        _transport: HTTPTransport | None = None,
    ):
        if type(cost_limit_micro_usd) is not int or cost_limit_micro_usd <= 0:
            raise AgentGitError(ErrorCode.CONFIGURATION, "真实模型必须声明正整数 USD 费用预算")
        self.options, self.cost_limit_micro_usd = options, cost_limit_micro_usd
        self.signature = model_signature(options)
        self._transport = _transport or FixedOpenAIHTTPS()

    def invoke(
        self,
        payload: bytes,
        *,
        timeout_ms: int | None = None,
        max_reply_bytes: int = 16_384,
        cost_limit_micro_usd: int | None = None,
    ) -> bytes:
        if type(max_reply_bytes) is not int or not 2048 <= max_reply_bytes <= 1_048_576:
            raise AgentGitError(ErrorCode.CONFIGURATION, "真实模型结果封装需要 2 KiB..1 MiB")
        if len(payload) > 1_048_576:
            raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "模型逻辑请求超过字节上限")
        request = parse_contract(payload, ModelRequest)
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not re.fullmatch(r"[!-~]{16,512}", api_key):
            raise AgentGitError(ErrorCode.CONFIGURATION, "Worker 未配置有效的 OPENAI_API_KEY")
        common = response_input(request, self.options)
        count_body = encode(common)
        if api_key.encode() in count_body:
            raise AgentGitError(ErrorCode.CONFIGURATION, "模型请求包含当前凭据，已拒绝发送")
        # Runtime 仍须在调用前持久化同等上界；这里防止 Adapter 被单独调用时绕过。
        reserve = price_micro_usd(self.options.max_input_tokens, self.options.max_output_tokens)
        available_cost = (
            self.cost_limit_micro_usd
            if cost_limit_micro_usd is None
            else min(self.cost_limit_micro_usd, cost_limit_micro_usd)
        )
        if reserve > available_cost:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "模型调用费用预留不足")
        duration = (
            self.options.timeout_ms
            if timeout_ms is None
            else min(timeout_ms, self.options.timeout_ms)
        )
        if type(duration) is not int or duration <= 0:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "模型调用没有剩余墙钟预算")
        deadline = time.monotonic() + duration / 1000
        client_id, count_client_id = uuid.uuid4().hex, uuid.uuid4().hex
        count_id: str | None = None
        counted: int | None = None

        def saved(
            *,
            http: HTTPReply | None,
            data: dict[str, Any] | None = None,
            decision: dict[str, Any] | None = None,
            generated: bool = False,
            code: Literal[
                "model_error", "model_empty", "validation", "configuration", "context_limit"
            ] = "configuration",
            retry: bool = False,
            delay: int | None = None,
            price_verified: bool = True,
        ) -> bytes:
            data = data or {}
            meter = _meter(
                data.get("usage"),
                self.options,
                counted,
                generated=generated,
                price_verified=price_verified,
            )
            result = ModelReply(
                disposition="decision" if decision is not None else "retry" if retry else "stop",
                decision=decision,
                error_code=None if decision is not None else code,
                meter=meter,
                response_id=_identity(data.get("id")),
                request_id=_identity(http.request_id) if http is not None else None,
                count_request_id=count_id,
                client_request_id=client_id,
                count_client_request_id=count_client_id,
                http_status=http.status if http is not None else None,
                retry_after_ms=delay,
            )
            encoded = encode(result.model_dump(mode="json"))
            if len(encoded) > max_reply_bytes:
                # 仍保留计量；拒绝把截断 JSON 交给 Runtime 后又失去 usage。
                result = result.model_copy(
                    update={
                        "decision": None,
                        "disposition": "stop",
                        "error_code": "validation",
                    }
                )
                encoded = encode(result.model_dump(mode="json"))
            if len(encoded) > max_reply_bytes:
                result = result.model_copy(
                    update={
                        "response_id": None,
                        "request_id": None,
                        "count_request_id": None,
                    }
                )
                encoded = encode(result.model_dump(mode="json"))
            return encoded

        def post(path: str, body: bytes, *, generating: bool) -> HTTPReply | None:
            try:
                reply = self._transport.post(
                    path,
                    body,
                    api_key=api_key,
                    client_request_id=client_id if generating else count_client_id,
                    deadline=deadline,
                    max_bytes=min(8_388_608, max_reply_bytes * 8 + 65_536),
                )
                return HTTPReply(
                    reply.status,
                    reply.body,
                    None if api_key in (reply.request_id or "") else reply.request_id,
                    None if api_key in (reply.retry_after or "") else reply.retry_after,
                )
            except TransportFailure as exc:
                if generating and exc.sent:
                    raise AgentGitError(
                        ErrorCode.OUTCOME_UNKNOWN, "模型请求可能已发送，保留预留且不自动重发"
                    ) from None
                return None

        def rejected(http: HTTPReply, *, generating: bool) -> bytes:
            if generating and (http.status >= 500 or http.status == 408):
                raise AgentGitError(
                    ErrorCode.OUTCOME_UNKNOWN, "供应商生成状态未知，保留预留且不自动重发"
                )
            delay = _retry_delay(http.retry_after) if http.status == 429 else None
            retry = (
                http.status == 429
                and delay is not None
                and (time.monotonic() + delay / 1000 < deadline)
            )
            return saved(
                http=http,
                code="model_error" if retry else "configuration",
                retry=retry,
                delay=delay,
            )

        count = post("/v1/responses/input_tokens", count_body, generating=False)
        if count is None:
            return saved(http=None, code="model_error", retry=True)
        count_id = _identity(count.request_id)
        if count.status != 200:
            return rejected(count, generating=False)
        data = _json_object(count.body, api_key)
        if data is None or data.get("object") != "response.input_tokens":
            return saved(http=count)
        counted = _integer(data.get("input_tokens"))
        if counted is None:
            return saved(http=count)
        if counted > self.options.max_input_tokens:
            return saved(http=count, code="context_limit")
        body = encode(
            {
                **common,
                "max_output_tokens": self.options.max_output_tokens,
                "service_tier": "default",
                "store": False,
                "stream": False,
            }
        )
        http = post("/v1/responses", body, generating=True)
        if http is None:
            return saved(http=None, code="model_error", retry=True)
        if http.status != 200:
            return rejected(http, generating=True)
        data = _json_object(http.body, api_key)
        if data is None:
            return saved(http=http, generated=True, price_verified=False)
        price_verified = data.get("model") == self.options.model and (
            data.get("service_tier") == "default"
        )
        meter = _meter(
            data.get("usage"), self.options, counted, generated=True, price_verified=price_verified
        )
        common_saved: _SavedReply = {
            "http": http,
            "data": data,
            "generated": True,
            "price_verified": price_verified,
        }
        if (
            not meter.price_verified
            or meter.basis != "provider_usage"
            or meter.accounted_input_tokens > self.options.max_input_tokens
            or meter.accounted_output_tokens > self.options.max_output_tokens
            or data.get("object") != "response"
            or data.get("status") != "completed"
            or data.get("error") is not None
            or data.get("incomplete_details") is not None
        ):
            return saved(**common_saved)
        output = data.get("output")
        texts: list[str] = []
        if not isinstance(output, list):
            return saved(**common_saved, code="validation")
        for item in output:
            if not isinstance(item, dict):
                return saved(**common_saved, code="validation")
            if item.get("type") == "reasoning":
                continue
            if (item.get("type"), item.get("role"), item.get("status")) != (
                "message",
                "assistant",
                "completed",
            ) or not isinstance(item.get("content"), list):
                return saved(**common_saved, code="validation")
            for part in item["content"]:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "output_text"
                    or (not isinstance(part.get("text"), str))
                ):
                    return saved(**common_saved, code="validation")
                texts.append(part["text"])
        if len(texts) != 1:
            return saved(**common_saved, code="model_empty" if not texts else "validation")
        try:
            decision = parse_structured_decision(texts[0], request)
        except (AgentGitError, ValueError, UnicodeError, RecursionError):
            return saved(**common_saved, code="validation")
        return saved(**common_saved, decision=decision)

    def invoke_bounded(
        self,
        payload: bytes,
        *,
        timeout_ms: int,
        max_reply_bytes: int,
        cost_limit_micro_usd: int,
    ) -> bytes:
        return self.invoke(
            payload,
            timeout_ms=timeout_ms,
            max_reply_bytes=max_reply_bytes,
            cost_limit_micro_usd=cost_limit_micro_usd,
        )
