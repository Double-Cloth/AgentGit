"""S1-T03/T04：协议黄金向量与拒绝规则。"""

import json
from pathlib import Path

import pytest

from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/canonical"
VECTORS = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_golden(vector):
    payload = bytes.fromhex(vector["canonical_hex"])
    if vector["object_type"] != "blob":
        assert encode(decode(vector["input_json"].encode("utf-8"))) == payload
    assert object_digest(ObjectType(vector["object_type"]), payload) == vector["digest"]


def test_order_unicode_and_array():
    assert encode({"b": 1, "a": 2}) == encode({"a": 2, "b": 1})
    assert encode([1, 2]) != encode([2, 1])
    assert encode("é") != encode("e\u0301")
    assert encode({"\U0001f600": 1, "\ue000": 2}) == '{"\ue000":2,"😀":1}'.encode()
    assert encode(decode(b"-0")) == b"0"
    assert encode('\b\f\n\r\t\x00/\\"') == b'"\\b\\f\\n\\r\\t\\u0000/\\\\\\""'


@pytest.mark.parametrize(
    "payload",
    [
        b'{"x":1,"x":2}',
        b'{"a":{"x":1,"x":2}}',
        b'"\\ud800"',
        b'"\\udc00"',
        b"0.1",
        b"1e0",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"9007199254740992",
        b"-9007199254740992",
        b"\xef\xbb\xbf{}",
        b"\xff",
        b"[",
        b"01",
        b"[" * 102 + b"0" + b"]" * 102,
    ],
)
def test_reject_invalid_json(payload):
    with pytest.raises(AgentGitError) as caught:
        decode(payload)
    assert caught.value.code == ErrorCode.VALIDATION


@pytest.mark.parametrize("value", [1.0, float("nan"), 2**53, {1: "x"}, "\ud800", (1, 2)])
def test_reject_invalid_values(value):
    with pytest.raises(AgentGitError):
        encode(value)


def test_hash_domain_separation():
    assert object_digest(ObjectType.STATE, b"x") != object_digest(ObjectType.BLOB, b"x")
    with pytest.raises(AgentGitError, match="codec"):
        object_digest(ObjectType.STATE, b"x", codec="AGJSON-v2")
    with pytest.raises(AgentGitError, match="对象类型"):
        object_digest("blob\0other", b"x")
