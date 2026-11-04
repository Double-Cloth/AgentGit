"""MCP 端点、闭合 Schema、本地效果与绑定的拒绝边界。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import MCPBinding, MCPServer
from agentgit.core.mcp_schema import check_schema, validate_value
from agentgit.core.models import EffectClass

TOOL = json.loads((Path(__file__).parents[1] / "fixtures/mcp/v1/tools.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8000/mcp",
        "https://127.0.0.1:8000/mcp",
        "http://10.0.0.1:8000/mcp",
        "http://example.com:8000/mcp",
        "http://[::1]:8000/mcp",
        "http://127.0.0.1/mcp",
        "http://127.0.0.1:8000/mcp/",
        "http://127.0.0.1:8000/mcp?target=other",
        "http://127.0.0.1:8000/mcp#fragment",
        "http://user:secret@127.0.0.1:8000/mcp",
        "http://127.0.0.1:0/mcp",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:08000/mcp",
    ],
)
def test_registered_endpoint_cannot_be_retargeted(endpoint):
    with pytest.raises(ValidationError):
        MCPServer(server_identity="fixture", endpoint=endpoint)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "number"},
        {"$ref": "https://example.com/schema"},
        {"type": "string", "pattern": "a+"},
        {"type": "string", "x-mcp-header": "X-Authorization"},
        {"type": ["null", "string"]},
        {"type": "object", "properties": {}},
        {"type": "object", "properties": {}, "additionalProperties": False, "required": ["x"]},
        {"type": "string", "minLength": True},
        {"type": "string", "minLength": 2, "maxLength": 1},
        {"type": "array"},
        {"type": "boolean", "enum": []},
        {"type": "string", "enum": ["a", "a"]},
    ],
)
def test_unsupported_constraints_fail_closed(schema):
    with pytest.raises(AgentGitError) as caught:
        check_schema(schema)
    assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "value", [{}, {"value": 1}, {"value": "x", "extra": 1}, {"value": "x" * 101}]
)
def test_arguments_are_checked_before_call(value):
    with pytest.raises(AgentGitError) as caught:
        validate_value(TOOL["inputSchema"], value)
    assert caught.value.code == ErrorCode.VALIDATION


def test_pin_uses_local_effect_not_remote_readonly_hint():
    binding = MCPBinding(
        server=MCPServer(server_identity="fixture", endpoint="http://127.0.0.1:8000/mcp"),
        tool_name="echo",
        definition_json=encode(TOOL).decode(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
    )
    assert not binding.rule().preauthorized
    assert binding.rule().scope == "external"
    assert binding.rule().effect == EffectClass.NON_IDEMPOTENT_WRITE
    changed = json.loads(binding.definition_json)
    changed["inputSchema"]["properties"]["value"]["maxLength"] = 99
    other = MCPBinding(
        server=binding.server,
        tool_name="echo",
        definition_json=encode(changed).decode(),
        effect_class=binding.effect_class,
    )
    assert (
        other.signature() != binding.signature()
        and other.schema_digest() != binding.schema_digest()
    )
    changed = binding.definition()
    changed["inputSchema"] = {}
    assert binding.definition() == TOOL


def test_supported_nested_schema_validates_exact_types_and_bounds():
    schema = {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "items": {"type": "integer", "minimum": 1, "maximum": 2},
    }
    check_schema(schema)
    validate_value(schema, [1, 2])
    for invalid in [[True], [0], [3], [], [1, 1, 1]]:
        with pytest.raises(AgentGitError):
            validate_value(schema, invalid)


def test_stdio_and_command_are_not_supported_configuration():
    with pytest.raises(ValidationError):
        MCPServer(
            server_identity="fixture", endpoint="http://127.0.0.1:8000/mcp", command="cmd.exe"
        )
