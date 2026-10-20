"""AGJSON-v1 受限编码及带域隔离的内容摘要，不等同于 JCS。"""

import hashlib
import json
from enum import StrEnum
from typing import Any

from agentgit.core.errors import AgentGitError, ErrorCode

CODEC = "AGJSON-v1"
MAX_INTEGER = 9_007_199_254_740_991


class ObjectType(StrEnum):
    STATE = "state"
    ENVIRONMENT = "environment"
    COMMIT = "commit"
    EVENT = "event"
    EVENT_MANIFEST = "event_manifest"
    CALL = "call"
    ARTIFACT_MANIFEST = "artifact_manifest"
    BLOB = "blob"


def _invalid(message: str) -> AgentGitError:
    return AgentGitError(ErrorCode.VALIDATION, message)


def _validate(value: Any, depth: int = 0) -> None:
    if depth > 100:
        raise _invalid("AGJSON 嵌套深度超过 100")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_INTEGER:
            raise _invalid("整数超出 AGJSON 安全范围")
    elif type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise _invalid("字符串含孤立 Unicode 代理项")
    elif type(value) is list:
        for item in value:
            _validate(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _invalid("AGJSON object key 必须是字符串")
            _validate(key, depth + 1)
            _validate(item, depth + 1)
    else:
        raise _invalid("AGJSON 禁止浮点数及非 JSON 类型")


def encode(value: Any) -> bytes:
    _validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("JSON 含重复 key")
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise _invalid("AGJSON 禁止 NaN 和 Infinity")


def decode(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
        if text.startswith("\ufeff"):
            raise _invalid("AGJSON 禁止 UTF-8 BOM")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constant)
        _validate(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("无效的 AGJSON 文档") from exc


def object_digest(object_type: ObjectType, payload: bytes, *, codec: str = CODEC) -> str:
    if not isinstance(object_type, ObjectType):
        raise _invalid("未知对象类型")
    if codec != CODEC:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不支持的 codec 版本")
    header = f"agentgit\0{codec}\0{object_type.value}\0{len(payload)}\0".encode()
    return hashlib.sha256(header + payload).hexdigest()
