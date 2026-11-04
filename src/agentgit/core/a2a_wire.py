"""固定 A2A 配置的有界 wire 校验；不代替远端任务的本地归属核对。"""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from agentgit.core.a2a import A2ABinding, _fields, _text, check_a2a_content
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.models import Contract, Digest

RemoteID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
TaskOperation = Literal["send", "get", "cancel"]
STATES = {
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
}


def remote_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ValueError("A2A 远端标识不在安全路径范围")
    return value


def parts(value: Any, *, allow_data: bool = True) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("A2A Part 数量无效")
    for part in value:
        item = _fields(part, set(), {"text", "data", "mediaType"})
        if ("text" in item) == ("data" in item):
            raise ValueError("A2A Part 必须且只能包含一种受支持内容")
        if "text" in item:
            _text(item["text"], 32_768)
            if item.get("mediaType", "text/plain") != "text/plain":
                raise ValueError("A2A 文本媒体类型无效")
        elif not allow_data or item.get("mediaType", "application/json") != "application/json":
            raise ValueError("A2A 结构化内容不在声明范围")
        # AGJSON 另拒绝浮点数、重复 key 和过深嵌套，不使用远端文件或任意解码器。
        if len(encode(item)) > 49_152:
            raise ValueError("A2A Part 超过内容上限")
        check_a2a_content(item)


def message(value: Any, *, task_id: str | None = None, context_id: str | None = None) -> None:
    item = _fields(value, {"messageId", "role", "parts"}, {"taskId", "contextId"})
    remote_id(item["messageId"])
    if item["role"] not in ("ROLE_USER", "ROLE_AGENT"):
        raise ValueError("A2A 消息角色无效")
    parts(item["parts"], allow_data=item["role"] == "ROLE_AGENT")
    for field, expected in (("taskId", task_id), ("contextId", context_id)):
        if field in item:
            remote_id(item[field])
            if expected is not None and item[field] != expected:
                raise ValueError("A2A 消息与确切任务／上下文不符")
        elif item["role"] == "ROLE_AGENT" and expected is not None:
            raise ValueError("A2A Agent 消息缺少任务／上下文")


def task(value: Any) -> None:
    item = _fields(value, {"id", "contextId", "status"}, {"history", "artifacts"})
    task_id, context_id = remote_id(item["id"]), remote_id(item["contextId"])
    status = _fields(item["status"], {"state"}, {"message", "timestamp"})
    if not isinstance(status["state"], str) or status["state"] not in STATES:
        raise ValueError("A2A 未知任务状态")
    if "message" in status:
        message(status["message"], task_id=task_id, context_id=context_id)
    if "timestamp" in status:
        stamp = status["timestamp"]
        if (
            not isinstance(stamp, str)
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z", stamp
            )
            is None
        ):
            raise ValueError("A2A 状态时间必须为 UTC 时间戳")
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    history = item.get("history", [])
    artifacts = item.get("artifacts", [])
    if not isinstance(history, list) or len(history) > 32:
        raise ValueError("A2A 消息历史超过声明范围")
    seen = set()
    for turn in history:
        message(turn, task_id=task_id, context_id=context_id)
        if turn["messageId"] in seen:
            raise ValueError("A2A 消息历史重复")
        seen.add(turn["messageId"])
    if not isinstance(artifacts, list) or len(artifacts) > 8:
        raise ValueError("A2A 产物数量超过声明范围")
    seen = set()
    for artifact in artifacts:
        entry = _fields(artifact, {"artifactId", "parts"}, {"name", "description"})
        identifier = remote_id(entry["artifactId"])
        if identifier in seen:
            raise ValueError("A2A 产物标识重复")
        seen.add(identifier)
        for field in ("name", "description"):
            if field in entry:
                _text(entry[field])
        parts(entry["parts"])
    check_a2a_content(item)


def binding_digest(binding: A2ABinding) -> str:
    return object_digest(ObjectType.BLOB, encode(binding.model_dump(mode="json")))


class A2ATaskReference(Contract):
    # 此引用只绑定已校验回执；应用服务仍须核对确切 Run／Commit 来源及项目权限。
    binding_ref: Digest
    response_ref: Digest
    task_id: RemoteID
    context_id: RemoteID


class A2ACall(Contract):
    operation: TaskOperation
    body_json: Annotated[str, Field(max_length=65_536)]
    task: A2ATaskReference | None = None

    def body(self) -> dict[str, Any]:
        raw = self.body_json.encode("utf-8")
        if len(raw) > 65_536:
            raise ValueError("A2A 请求超过字节上限")
        value = decode(raw)
        if not isinstance(value, dict) or encode(value) != raw:
            raise ValueError("A2A 请求必须为 canonical object")
        check_a2a_content(value)
        return value

    @model_validator(mode="after")
    def supported_request(self) -> "A2ACall":
        value = self.body()
        if self.operation == "send":
            _fields(value, {"message", "configuration"})
            message(value["message"])
            if value["message"]["role"] != "ROLE_USER":
                raise ValueError("A2A 本地只能发送用户消息")
            configuration = _fields(
                value["configuration"], {"acceptedOutputModes", "returnImmediately"}
            )
            if (
                configuration["acceptedOutputModes"] != ["text/plain", "application/json"]
                or configuration["returnImmediately"] is not True
            ):
                raise ValueError("A2A 仅支持立即返回的有界轮询任务")
            if self.task is None:
                if {"taskId", "contextId"} & value["message"].keys():
                    raise ValueError("A2A 首次发送不能夹带任意任务／上下文")
            elif (
                value["message"].get("taskId") != self.task.task_id
                or value["message"].get("contextId") != self.task.context_id
            ):
                raise ValueError("A2A 追加消息必须绑定确切任务／上下文")
        elif self.task is None or value != {"id": self.task.task_id}:
            raise ValueError("A2A 查询／取消必须绑定确切回执任务")
        return self

    def validate_binding(self, binding: A2ABinding) -> None:
        if self.task is not None and self.task.binding_ref != binding_digest(binding):
            raise ValueError("A2A 任务引用属于不同端点／主体／卡片")


class A2AResult(Contract):
    kind: Literal["a2a_task_result/v1"] = "a2a_task_result/v1"
    operation: TaskOperation
    binding_ref: Digest
    request_ref: Digest
    task_json: Annotated[str, Field(max_length=1_048_576)]

    def value(self) -> dict[str, Any]:
        raw = self.task_json.encode("utf-8")
        value = decode(raw)
        if not isinstance(value, dict) or encode(value) != raw:
            raise ValueError("A2A 任务结果必须为 canonical object")
        task(value)
        return value

    @model_validator(mode="after")
    def supported_result(self) -> "A2AResult":
        self.value()
        return self

    def reference(self) -> A2ATaskReference:
        value = self.value()
        return A2ATaskReference(
            binding_ref=self.binding_ref,
            response_ref=object_digest(ObjectType.BLOB, encode(self.model_dump(mode="json"))),
            task_id=value["id"],
            context_id=value["contextId"],
        )
