"""本地协议绑定与原效果策略；不使用网络或自称完成耐久适配。"""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.core.a2a import A2ABinding, A2AServer, check_a2a_content
from agentgit.core.canonical import encode
from agentgit.core.models import EffectClass, parse_contract

CARD = json.loads(
    (Path(__file__).parents[1] / "fixtures/a2a/v1/agent-card.json").read_text("utf-8")
)


def server(**kwargs):
    return A2AServer(
        server_identity="local-research",
        principal_id="operator",
        endpoint="http://127.0.0.1:1",
        **kwargs,
    )


def binding(card=None, configured=None):
    return A2ABinding(server=configured or server(), card_json=encode(card or CARD).decode())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8000",
        "http://10.0.0.1:8000",
        "http://example.com:8000",
        "http://[::1]:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:08000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000/a2a",
        "http://127.0.0.1:8000?x=1",
        "http://127.0.0.1:8000#fragment",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000\n",
        "http://127.1:8000",
    ],
)
def test_endpoint_cannot_retarget_registered_root(endpoint):
    with pytest.raises(ValidationError):
        A2AServer(server_identity="local", principal_id="operator", endpoint=endpoint)


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": "0.3"},
        {"protocol_version": "1.0.1"},
        {"upstream_release": "latest"},
        {"timeout_ms": True},
        {"timeout_ms": 19},
        {"timeout_ms": 30001},
        {"max_response_bytes": 127},
        {"max_response_bytes": 1048577},
        {"principal_id": ""},
        {"principal_id": "a/b"},
        {"principal_id": "a" * 65},
        {"server_identity": "a:b"},
        {"server_identity": "a" * 65},
        {"credential": "should-not-be-stored"},
    ],
)
def test_versions_identity_and_limits_are_strict(changes):
    value = server().model_dump(mode="json")
    value.update(changes)
    with pytest.raises(ValidationError):
        A2AServer.model_validate(value)


def test_binding_is_canonical_immutable_and_uses_original_effect_rules():
    bound = binding()
    assert parse_contract(encode(bound.model_dump(mode="json")), A2ABinding) == bound
    snapshot = bound.card()
    snapshot["name"] = "changed"
    assert bound.card()["name"] == CARD["name"]
    assert bound.effect("send") == EffectClass.NON_IDEMPOTENT_WRITE
    assert bound.effect("cancel") == EffectClass.IDEMPOTENT_WRITE
    for operation in ("discover", "get", "send", "cancel"):
        rule = bound.rule(operation)
        assert rule.scope == "external"
        assert rule.preauthorized is (operation in ("discover", "get"))
    assert len({bound.signature(op) for op in ("discover", "get", "send", "cancel")}) == 4
    with pytest.raises(ValidationError):
        bound.profile = "changed"


def test_longest_registered_names_still_fit_original_provider_limit():
    value = server().model_dump(mode="json")
    value.update(server_identity="s" * 64, principal_id="p" * 64)
    bound = binding(configured=A2AServer.model_validate(value))
    for operation in ("discover", "get", "send", "cancel"):
        assert len(bound.rule(operation).provider) <= 200


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://127.0.0.1:2"},
        {"protocolBinding": "JSONRPC"},
        {"protocolVersion": "0.3"},
        {"tenant": "other"},
    ],
)
def test_remote_card_cannot_expand_interface(changes):
    card = copy.deepcopy(CARD)
    card["supportedInterfaces"][0].update(changes)
    with pytest.raises(ValidationError):
        binding(card)


@pytest.mark.parametrize(
    "field,value",
    [
        ("streaming", True),
        ("pushNotifications", True),
        ("extendedAgentCard", True),
        ("streaming", 0),
        ("extensions", []),
    ],
)
def test_remote_capabilities_cannot_enable_unsupported_channels(field, value):
    card = copy.deepcopy(CARD)
    card["capabilities"][field] = value
    with pytest.raises(ValidationError):
        binding(card)


@pytest.mark.parametrize(
    "field,value",
    [
        ("securityRequirements", []),
        ("securitySchemes", {}),
        ("defaultOutputModes", ["application/octet-stream"]),
        ("skills", []),
        ("iconUrl", "http://127.0.0.1:2/icon"),
        ("name", ""),
    ],
)
def test_remote_card_cannot_expand_security_or_content(field, value):
    card = copy.deepcopy(CARD)
    card[field] = value
    with pytest.raises(ValidationError):
        binding(card)


def test_canonical_card_and_principal_are_part_of_signature():
    with pytest.raises(ValidationError):
        A2ABinding(server=server(), card_json=json.dumps(CARD))
    original = binding()
    card = copy.deepcopy(CARD)
    card["description"] = "different declaration"
    assert binding(card).signature("send") != original.signature("send")
    value = server().model_dump(mode="json")
    value["principal_id"] = "other-operator"
    other = binding(configured=A2AServer.model_validate(value))
    assert other.signature("send") != original.signature("send")
    assert other.provider("send") != original.provider("send")


@pytest.mark.parametrize(
    "value",
    [
        {"Authorization": "value"},
        {"nested": [{"access-token": "value"}]},
        {"credentials": "value"},
        {"sensitive-value": "other"},
        ["echo-sensitive-value"],
    ],
)
def test_secret_fields_or_exact_runtime_secret_are_rejected_without_echo(value):
    with pytest.raises(ValueError, match="A2A 内容不能") as error:
        check_a2a_content(value, "sensitive-value")
    assert "sensitive-value" not in str(error.value)


def test_public_protocol_descriptions_do_not_count_as_credentials():
    check_a2a_content(CARD, "sensitive-value")
