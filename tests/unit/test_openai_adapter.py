"""固定供应商协议的离线验收；不读取真实凭据，不允许外网请求。"""

import copy
import json
import time

import pytest
from pydantic import ValidationError

from agentgit.adapters.model_http import HTTPReply, TransportFailure
from agentgit.adapters.model_protocol import decision_schema, model_signature
from agentgit.adapters.openai import OpenAIResearchModel
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.model_usage import ModelReply, OpenAIOptions, price_micro_usd
from agentgit.core.models import parse_contract
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    BudgetLimits,
    BudgetUsage,
    ModelRequest,
    ResearchGoal,
)

TEST_KEY = "sk-agentgit-public-offline-fixture-only"


def request(phase="plan", **changes):
    return encode(
        ModelRequest(
            prompt="比较合成资料",
            phase=phase,
            state=AgentState(goal=ResearchGoal(text="固定小任务")),
            **changes,
        ).model_dump(mode="json")
    )


def decision(kind="plan", **changes):
    return {
        "kind": kind,
        "plan": {
            "schema_version": 1,
            "nodes": [
                {
                    "schema_version": 1,
                    "key": "search",
                    "kind": "search",
                    "dependencies": [],
                    "source_slot": None,
                }
            ],
        }
        if kind == "plan"
        else None,
        "node_key": None,
        "tool": None,
        "arguments_json": None,
        "summary": None,
        **changes,
    }


def response(*, structured=None, **changes):
    return {
        "object": "response",
        "id": "resp_fixture",
        "model": OpenAIOptions().model,
        "status": "completed",
        "service_tier": "default",
        "error": None,
        "incomplete_details": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(decision() if structured is None else structured),
                    }
                ],
            }
        ],
        **changes,
    }


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, path, body, **options):
        self.calls.append((path, json.loads(body), options))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def count_reply(**changes):
    return HTTPReply(
        200,
        encode({"object": "response.input_tokens", "input_tokens": 100, **changes}),
        "req_count",
    )


def invoke(
    monkeypatch, data=None, *, replies=None, phase="plan", options=None, max_reply_bytes=16_384
):
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    transport = Transport(
        *(
            replies
            if replies is not None
            else [
                count_reply(),
                HTTPReply(200, json.dumps(data or response()).encode(), "req_generation"),
            ]
        )
    )
    model = OpenAIResearchModel(
        options or OpenAIOptions(), cost_limit_micro_usd=1_000_000, _transport=transport
    )
    payload = model.invoke(request(phase), max_reply_bytes=max_reply_bytes)
    assert TEST_KEY.encode() not in payload
    return parse_contract(payload, ModelReply), transport


def test_default_mock_contracts_and_signature_remain_exact():
    from agentgit.adapters.mock import MockResearchModel

    assert set(AgentConfig().model_dump()) == {
        "schema_version",
        "kind",
        "model",
        "prompt",
        "tools",
        "execution_policy",
        "limits",
    }
    assert set(BudgetLimits().model_dump()) == {
        "schema_version",
        "max_steps",
        "max_model_calls",
        "max_tool_calls",
        "max_replans",
        "max_tokens",
        "max_wall_ms",
        "max_response_bytes",
        "max_context_bytes",
        "max_artifact_bytes",
        "max_files",
    }
    assert BudgetUsage().model_dump(mode="json") == {
        "schema_version": 1,
        "steps": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "tokens": 0,
        "replans": 0,
        "reserved_tokens": 0,
        "cost_micro_units": 0,
        "usage_kind": "mock_utf8_bytes",
    }
    assert MockResearchModel.signature == object_digest(
        ObjectType.CALL, encode({"schema_version": 1, "model": "mock-research/v1"})
    )


def test_signature_is_pure_stable_and_binds_limits_schema_and_pricebook(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    options = OpenAIOptions()
    assert model_signature(options) == model_signature(OpenAIOptions())
    assert model_signature(options) != model_signature(OpenAIOptions(max_output_tokens=4096))
    assert model_signature(options) != model_signature(OpenAIOptions(reasoning_effort="low"))
    original = decision_schema()
    assert original["type"] == "object" and "anyOf" not in original

    def inspect(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            assert "default" not in value
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(original)


@pytest.mark.parametrize(
    "values, expected",
    [
        ((0, 0, 0), 0),
        ((1, 0, 0), 1),
        ((0, 1, 0), 5),
        ((100, 20, 40), 138),
        ((1_000_000, 1_000_000, 0), 5_250_000),
        ((1_000_000, 0, 1_000_000), 75_000),
    ],
)
def test_integer_price_counts_cache_and_reasoning_once(values, expected):
    assert price_micro_usd(*values) == expected


@pytest.mark.parametrize("values", [(True, 0), (-1, 1), (1, 1, 2), (1.0, 0)])
def test_invalid_price_inputs_rejected(values):
    with pytest.raises(ValueError):
        price_micro_usd(*values)


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "gpt-5.4-mini"},
        {"pricebook": "floating"},
        {"base_url": "http://evil"},
        {"api_key": TEST_KEY},
        {"max_input_tokens": True},
        {"max_output_tokens": 0},
        {"timeout_ms": 120_001},
        {"service_tier": "auto"},
    ],
)
def test_options_reject_alias_url_secret_and_unbounded_values(changes):
    with pytest.raises(ValidationError):
        OpenAIOptions(**changes)


def test_success_counts_exact_input_then_generates_once_with_fixed_contract(monkeypatch):
    reply, transport = invoke(monkeypatch)
    assert reply.disposition == "decision" and reply.decision["kind"] == "plan"
    assert reply.meter.basis == "provider_usage"
    assert (reply.meter.input_tokens, reply.meter.output_tokens, reply.meter.reasoning_tokens) == (
        100,
        20,
        5,
    )
    assert reply.meter.estimated_cost_micro_usd == 138
    assert reply.request_id == "req_generation" and reply.count_request_id == "req_count"
    assert [call[0] for call in transport.calls] == ["/v1/responses/input_tokens", "/v1/responses"]
    counted, generated = [call[1] for call in transport.calls]
    assert all(generated[name] == value for name, value in counted.items())
    assert generated["store"] is False and generated["stream"] is False
    assert generated["truncation"] == "disabled" and generated["service_tier"] == "default"
    assert "tools" not in generated and "previous_response_id" not in generated
    assert transport.calls[0][2]["deadline"] == transport.calls[1][2]["deadline"]
    assert transport.calls[0][2]["client_request_id"] == reply.count_client_request_id
    assert transport.calls[1][2]["client_request_id"] == reply.client_request_id
    assert reply.count_client_request_id != reply.client_request_id


def test_local_tool_validation_accepts_known_arguments_without_running_tool(monkeypatch):
    payload = decision(
        "tool",
        node_key="search",
        tool="corpus_search",
        arguments_json='{"schema_version":1,"frameworks":["Aster"]}',
    )
    reply, _ = invoke(monkeypatch, response(structured=payload), phase="act")
    assert reply.decision["arguments"] == {"schema_version": 1, "frameworks": ["Aster"]}


@pytest.mark.parametrize(
    "structured,phase",
    [
        (decision("final", summary="提前结束"), "plan"),
        (decision(summary="混用字段"), "plan"),
        (decision("tool", node_key="x", tool="shell", arguments_json="{}"), "act"),
        (
            decision(
                "tool",
                node_key="x",
                tool="corpus_search",
                arguments_json='{"schema_version":1,"frameworks":[],"bypass":true}',
            ),
            "act",
        ),
        ({**decision(), "extra": True}, "plan"),
        (
            decision(
                plan={
                    "schema_version": 1,
                    "nodes": [
                        {
                            "schema_version": 1,
                            "key": "x",
                            "kind": "search",
                            "dependencies": ["x"],
                            "source_slot": None,
                        }
                    ],
                }
            ),
            "plan",
        ),
    ],
)
def test_invalid_phase_extra_fields_dag_and_tools_preserve_meter(monkeypatch, structured, phase):
    reply, transport = invoke(monkeypatch, response(structured=structured), phase=phase)
    assert reply.disposition == "stop" and reply.error_code == "validation"
    assert reply.meter.input_tokens == 100 and len(transport.calls) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"usage": None},
        {"usage": {}},
        {"usage": {"input_tokens": True, "output_tokens": 20, "total_tokens": 21}},
        {"usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 119}},
    ],
)
def test_missing_or_invalid_usage_is_not_zero_or_success(monkeypatch, changes):
    reply, _ = invoke(monkeypatch, response(**changes))
    assert reply.disposition == "stop" and reply.meter.basis == "conservative_bound"
    assert reply.meter.input_tokens is None and reply.meter.output_tokens is None
    assert reply.meter.accounted_input_tokens == 100
    assert reply.meter.accounted_output_tokens == OpenAIOptions().max_output_tokens
    assert reply.meter.estimated_cost_micro_usd > 0


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        {"status": "failed", "error": {"message": TEST_KEY}},
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "拒绝"}],
                }
            ]
        },
        {"output": [{"type": "function_call", "name": "shell", "arguments": "{}"}]},
        {"output": []},
    ],
)
def test_incomplete_refusal_and_hosted_tool_calls_cannot_succeed(monkeypatch, changes):
    reply, transport = invoke(monkeypatch, response(**changes))
    assert reply.disposition == "stop" and len(transport.calls) == 2
    assert reply.meter.estimated_cost_micro_usd > 0


@pytest.mark.parametrize("changes", [{"model": "other-model"}, {"service_tier": "priority"}])
def test_unexpected_pricing_does_not_claim_verified_cost(monkeypatch, changes):
    reply, _ = invoke(monkeypatch, response(**changes))
    assert reply.disposition == "stop" and reply.meter.price_verified is False
    assert reply.meter.input_tokens == 100


def test_cache_write_usage_is_preserved_but_not_priced_as_verified(monkeypatch):
    data = response()
    data["usage"]["input_tokens_details"]["cache_write_tokens"] = 10
    reply, _ = invoke(monkeypatch, data)
    assert reply.disposition == "stop"
    assert reply.meter.cache_write_tokens == 10 and not reply.meter.price_verified


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 413, 422])
@pytest.mark.parametrize("phase", ["count", "generation"])
def test_known_http_rejections_are_saved_without_body_or_retry(monkeypatch, status, phase):
    failure = HTTPReply(status, TEST_KEY.encode(), "req_error")
    replies = [failure] if phase == "count" else [count_reply(), failure]
    reply, transport = invoke(monkeypatch, replies=replies)
    assert reply.disposition == "stop" and reply.error_code == "configuration"
    assert reply.meter.basis == "not_generated" and reply.meter.estimated_cost_micro_usd == 0
    assert len(transport.calls) == len(replies)


def test_429_records_delay_and_leaves_bounded_retry_to_runtime(monkeypatch):
    reply, transport = invoke(
        monkeypatch, replies=[count_reply(), HTTPReply(429, b"{}", "req_limited", "2")]
    )
    assert reply.disposition == "retry" and reply.retry_after_ms == 2000
    assert reply.meter.basis == "not_generated" and len(transport.calls) == 2


@pytest.mark.parametrize("retry_after", ["garbage", "999999", "60"])
def test_retry_after_invalid_or_larger_than_deadline_stops(monkeypatch, retry_after):
    reply, _ = invoke(
        monkeypatch, replies=[count_reply(), HTTPReply(429, b"{}", retry_after=retry_after)]
    )
    assert reply.disposition == "stop"


@pytest.mark.parametrize(
    "failure", [TransportFailure(sent=True), HTTPReply(500, b"{}"), HTTPReply(408, b"{}")]
)
def test_sent_generation_unknown_never_resends(monkeypatch, failure):
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    transport = Transport(count_reply(), failure)
    model = OpenAIResearchModel(
        OpenAIOptions(), cost_limit_micro_usd=1_000_000, _transport=transport
    )
    with pytest.raises(AgentGitError) as error:
        model.invoke(request())
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert TEST_KEY not in str(error.value) and len(transport.calls) == 2


def test_connection_failure_before_generation_send_has_no_generation_usage(monkeypatch):
    reply, _ = invoke(monkeypatch, replies=[count_reply(), TransportFailure(sent=False)])
    assert reply.disposition == "retry" and reply.meter.basis == "not_generated"


def test_count_timeout_cannot_generate_or_claim_provider_usage(monkeypatch):
    reply, transport = invoke(monkeypatch, replies=[TransportFailure(sent=True)])
    assert reply.disposition == "retry" and reply.meter.input_tokens is None
    assert len(transport.calls) == 1


def test_input_token_count_limit_blocks_generation(monkeypatch):
    reply, transport = invoke(monkeypatch, replies=[count_reply(input_tokens=999_999)])
    assert reply.error_code == "context_limit" and len(transport.calls) == 1


@pytest.mark.parametrize("body", [b'{"object":"response","object":"response"}', b"not-json", b"[]"])
def test_malformed_json_cannot_hide_missing_usage(monkeypatch, body):
    reply, _ = invoke(monkeypatch, replies=[count_reply(), HTTPReply(200, body)])
    assert reply.disposition == "stop" and reply.meter.basis == "conservative_bound"


def test_escaped_secret_echo_and_header_values_are_not_persisted(monkeypatch):
    data = response()
    data["output"][0]["content"][0]["text"] = TEST_KEY
    encoded = json.dumps(data).replace("sk-agentgit", r"\u0073k-agentgit").encode()
    reply, _ = invoke(monkeypatch, replies=[count_reply(), HTTPReply(200, encoded, TEST_KEY)])
    assert reply.request_id is None and reply.decision is None
    assert reply.meter.basis == "conservative_bound"


def test_large_decision_rejected_without_truncating_meter(monkeypatch):
    data = response(structured=decision("final", summary="长中文" * 5000))
    reply, _ = invoke(monkeypatch, data, phase="act", max_reply_bytes=2048)
    assert reply.error_code == "validation" and reply.meter.input_tokens == 100
    assert len(encode(reply.model_dump(mode="json"))) <= 2048


def test_missing_key_and_insufficient_budget_do_not_touch_transport(monkeypatch):
    transport = Transport()
    model = OpenAIResearchModel(OpenAIOptions(), cost_limit_micro_usd=1, _transport=transport)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AgentGitError) as missing:
        model.invoke(request())
    assert missing.value.code == ErrorCode.CONFIGURATION
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    with pytest.raises(AgentGitError) as budget:
        model.invoke(request())
    assert budget.value.code == ErrorCode.BUDGET_EXHAUSTED and not transport.calls


def test_inconsistent_saved_meter_is_rejected(monkeypatch):
    reply, _ = invoke(monkeypatch)
    raw = copy.deepcopy(reply.model_dump(mode="json"))
    raw["meter"]["estimated_cost_micro_usd"] = 0
    with pytest.raises(AgentGitError):
        parse_contract(encode(raw), ModelReply)


def test_adapter_timeout_is_shared_across_count_and_generation(monkeypatch):
    reply, transport = invoke(monkeypatch, options=OpenAIOptions(timeout_ms=100))
    assert reply.disposition == "decision"
    deadline = transport.calls[0][2]["deadline"]
    assert time.monotonic() < deadline <= time.monotonic() + 0.1
    assert decode(request())["phase"] == "plan"


@pytest.mark.parametrize(
    "detail",
    [
        {"cached_tokens": -1},
        {"cached_tokens": 101},
        {"cache_write_tokens": True},
        {"cache_write_tokens": "0"},
    ],
)
def test_invalid_cache_details_fail_closed(monkeypatch, detail):
    data = response()
    data["usage"]["input_tokens_details"] = detail
    reply, _ = invoke(monkeypatch, data)
    assert reply.disposition == "stop" and reply.meter.basis == "conservative_bound"


def test_absurd_usage_cannot_overflow_canonical_currency_fields(monkeypatch):
    reply, _ = invoke(
        monkeypatch,
        response(
            usage={
                "input_tokens": 9_007_199_254_740_991,
                "output_tokens": 9_007_199_254_740_991,
                "total_tokens": 18_014_398_509_481_982,
            }
        ),
    )
    assert reply.disposition == "stop" and reply.meter.basis == "conservative_bound"


def test_secret_in_logical_request_is_blocked_before_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    transport = Transport()
    model = OpenAIResearchModel(
        OpenAIOptions(), cost_limit_micro_usd=1_000_000, _transport=transport
    )
    payload = json.loads(request())
    payload["prompt"] = TEST_KEY
    with pytest.raises(AgentGitError) as error:
        model.invoke(encode(payload))
    assert error.value.code == ErrorCode.CONFIGURATION and not transport.calls
    assert TEST_KEY not in str(error.value)


def test_long_server_ids_do_not_force_meter_truncation(monkeypatch):
    data = response(structured=decision("final", summary="长中文" * 5000), id="r" * 200)
    reply, _ = invoke(
        monkeypatch,
        phase="act",
        max_reply_bytes=2048,
        replies=[
            HTTPReply(
                200, encode({"object": "response.input_tokens", "input_tokens": 100}), "c" * 200
            ),
            HTTPReply(200, json.dumps(data).encode(), "h" * 200),
        ],
    )
    assert reply.disposition == "stop" and reply.meter.input_tokens == 100
    assert len(encode(reply.model_dump(mode="json"))) <= 2048
