"""有界 JSON Schema 子集；未实现的约束明确拒绝，不解析引用或联网。"""

from typing import Any

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode

_ANNOTATIONS = {"title", "description", "$schema"}
_FIELDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}


def check_schema(schema: object) -> None:
    if len(encode(schema)) > 65_536:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP Schema 超过上限")
    count = 0

    def visit(value: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if not isinstance(value, dict) or depth > 12 or count > 512:
            invalid()
        kind = value.get("type")
        if not isinstance(kind, str) or kind not in _FIELDS:
            invalid()
        if set(value) - (_FIELDS[kind] | _ANNOTATIONS | {"type", "enum"}):
            invalid()
        for key in ("title", "description"):
            if key in value and not isinstance(value[key], str):
                invalid()
        if (
            "$schema" in value
            and value["$schema"] != "https://json-schema.org/draft/2020-12/schema"
        ):
            invalid()
        if "enum" in value:
            items = value["enum"]
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                invalid()
            if len({encode(item) for item in items}) != len(items):
                invalid()
        if kind == "object":
            properties, required = value.get("properties"), value.get("required", [])
            if (
                not isinstance(properties, dict)
                or len(properties) > 100
                or value.get("additionalProperties") is not False
                or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required)
                or set(required) - set(properties)
            ):
                invalid()
            for child in properties.values():
                visit(child, depth + 1)
        elif kind == "array":
            visit(value.get("items"), depth + 1)
        for low, high in (
            ("minLength", "maxLength"),
            ("minItems", "maxItems"),
            ("minimum", "maximum"),
        ):
            for key in (low, high):
                if key in value and (
                    type(value[key]) is not int
                    or abs(value[key]) > 9_007_199_254_740_991
                    or (key not in {"minimum", "maximum"} and value[key] < 0)
                ):
                    invalid()
            if low in value and high in value and value[low] > value[high]:
                invalid()

    visit(schema, 0)


def invalid() -> None:
    raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "MCP Schema 含未支持或无效的约束")


def validate_value(schema: dict[str, Any], value: Any) -> None:
    """仅消费已通过 check_schema 的固定 Schema。"""
    kind = schema["type"]
    expected = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }[kind]
    if type(value) is not expected:
        mismatch()
    if "enum" in schema and encode(value) not in {encode(item) for item in schema["enum"]}:
        mismatch()
    if kind == "object":
        if set(value) - set(schema["properties"]) or set(schema.get("required", [])) - set(value):
            mismatch()
        for key, item in value.items():
            validate_value(schema["properties"][key], item)
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            mismatch()
        for item in value:
            validate_value(schema["items"], item)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1_048_576):
            mismatch()
    elif kind == "integer":
        if (
            not schema.get("minimum", -9_007_199_254_740_991)
            <= value
            <= schema.get("maximum", 9_007_199_254_740_991)
        ):
            mismatch()


def mismatch() -> None:
    raise AgentGitError(ErrorCode.VALIDATION, "MCP 内容不符合固定 Schema")
