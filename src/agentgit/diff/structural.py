"""确定的 JSON Pointer、二进制产物引用和 occurrence 差异。"""

from collections.abc import Iterator
from typing import Any

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import CallTrace
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import StructuralChange


def pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _changes(left: Any, right: Any, path: str) -> Iterator[StructuralChange]:
    if type(left) is not type(right):
        yield StructuralChange(path=path, operation="replace", before=left, after=right)
    elif isinstance(left, dict):
        for key in sorted(left.keys() | right.keys()):
            location = path + "/" + pointer_part(key)
            if key not in right:
                yield StructuralChange(path=location, operation="remove", before=left[key])
            elif key not in left:
                yield StructuralChange(path=location, operation="add", after=right[key])
            else:
                yield from _changes(left[key], right[key], location)
    elif isinstance(left, list):
        for index in range(max(len(left), len(right))):
            location = path + "/" + str(index)
            if index >= len(right):
                yield StructuralChange(path=location, operation="remove", before=left[index])
            elif index >= len(left):
                yield StructuralChange(path=location, operation="add", after=right[index])
            else:
                yield from _changes(left[index], right[index], location)
    elif left != right:
        yield StructuralChange(path=path, operation="replace", before=left, after=right)


def json_changes(
    left: Any, right: Any, *, path: str = "", maximum: int = 10000
) -> list[StructuralChange]:
    encode(left)
    encode(right)
    changes: list[StructuralChange] = []
    for change in _changes(left, right, path):
        if len(changes) >= maximum:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "差异数量超过有界比较上限")
        changes.append(change)
    return changes


def _calls(snapshot: StateSnapshot) -> dict[str, Any]:
    values = snapshot.data.get("messages", [])
    if not isinstance(values, list):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "调用轨迹必须为列表")
    calls: dict[str, Any] = {}
    for value in values:
        trace = parse_contract(encode(value), CallTrace)
        identity = encode(
            [
                trace.logical_step_id,
                trace.call_index,
                trace.attempt,
                trace.provider,
            ]
        ).decode("utf-8")
        if identity in calls:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "调用轨迹包含重复 occurrence")
        calls[identity] = trace.model_dump(mode="json")
    return calls


def snapshot_changes(left: StateSnapshot, right: StateSnapshot) -> list[StructuralChange]:
    def artifacts(snapshot: StateSnapshot) -> dict[str, Any]:
        return {
            item.path: {"object_ref": item.object_ref, "media_type": item.media_type}
            for item in snapshot.artifacts
        }

    # 三类差异都有明确入口；调用按 occurrence 对齐，数组按稳定索引展示。
    result = json_changes(left.data, right.data, path="/state")
    result += json_changes(
        artifacts(left), artifacts(right), path="/artifacts", maximum=10000 - len(result)
    )
    result += json_changes(_calls(left), _calls(right), path="/calls", maximum=10000 - len(result))
    return result
