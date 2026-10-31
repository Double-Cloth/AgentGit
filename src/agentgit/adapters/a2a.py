"""A2A 单次客户端与原调用网关边界；不自行轮询、重试或取得任务权限。"""

import os
import re
import time
from collections.abc import Callable
from typing import Any

from agentgit.adapters.a2a_http import A2ATransportFailure, request
from agentgit.core.a2a import A2ABinding, A2AServer, check_a2a_content
from agentgit.core.a2a_wire import A2ACall, A2AResult, binding_digest, task
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.runtime.ports import CallGateway, CallOutcome


def configured_client(server: A2AServer) -> "A2AClient":
    return A2AClient(server, credential_env=server.credential_env)


def _failed(operation: str, *, sent: bool) -> AgentGitError:
    if operation in ("send", "cancel") and sent:
        return AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "A2A 写请求没有可信回执，不得自动重发")
    return AgentGitError(ErrorCode.TOOL_ERROR, "A2A 未取得受支持的完整结果")


def _rejection(status: int, value: Any) -> AgentGitError | None:
    # 只识别固定协议中的确定拒绝；远端 message/details 原文均不进入错误。
    if not isinstance(value, dict) or set(value) != {"error"}:
        return None
    error = value["error"]
    if (
        not isinstance(error, dict)
        or set(error) - {"code", "message", "status", "details"}
        or type(error.get("code")) is not int
        or error["code"] != status
        or not isinstance(error.get("message"), str)
    ):
        return None
    details = error.get("details", [])
    if (
        not isinstance(details, list)
        or len(details) > 8
        or any(not isinstance(d, dict) or "@type" not in d for d in details)
    ):
        return None
    if status == 401:
        return AgentGitError(ErrorCode.UNAUTHORIZED, "A2A 运行时身份未获认证")
    if status == 403:
        return AgentGitError(ErrorCode.FORBIDDEN, "A2A 远端拒绝此身份的操作")
    if status == 409:
        return AgentGitError(ErrorCode.VERSION_CONFLICT, "A2A 确切消息与已有请求冲突")
    reasons = {
        d.get("reason")
        for d in details
        if d.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
        and d.get("domain") == "a2a-protocol.org"
        and isinstance(d.get("reason"), str)
    }
    if status == 404 and reasons == {"TASK_NOT_FOUND"}:
        return AgentGitError(ErrorCode.NOT_FOUND, "A2A 确切任务不存在或不可访问")
    if status == 400 and reasons == {"TASK_NOT_CANCELABLE"}:
        return AgentGitError(ErrorCode.FORBIDDEN, "A2A 确切任务当前不能取消")
    if status == 400 and reasons in ({"VERSION_NOT_SUPPORTED"}, {"UNSUPPORTED_OPERATION"}):
        return AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "A2A 远端不支持固定版本或操作")
    return None


class A2AClient:
    def __init__(self, server: A2AServer, *, credential_env: str | None = None):
        self.server = parse_contract(encode(server.model_dump(mode="json")), A2AServer)
        self._deadline: float | None = None
        self._guard: Callable[[], None] | None = None
        self._secret: str | None = None
        if credential_env is not None:
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,199}", credential_env) is None:
                raise AgentGitError(ErrorCode.CONFIGURATION, "A2A 凭据环境变量名称无效")
            secret = os.environ.get(credential_env)
            if secret is None or re.fullmatch(r"[\x21-\x7e]{1,4096}", secret) is None:
                raise AgentGitError(ErrorCode.CONFIGURATION, "A2A 运行时凭据缺失或无效")
            self._secret = secret

    def restrict_deadline(self, remaining_ms: int) -> None:
        if type(remaining_ms) is not int or not 1 <= remaining_ms <= 900_000:
            raise AgentGitError(ErrorCode.VALIDATION, "A2A 剩余期限无效")
        deadline = time.monotonic() + remaining_ms / 1000
        self._deadline = min(self._deadline, deadline) if self._deadline is not None else deadline

    def guard_requests(self, guard: Callable[[], None]) -> None:
        self._guard = guard

    def discover(self) -> A2ABinding:
        try:
            reply = request(
                self.server, "discover", b"", None, deadline=self._deadline, guard=self._guard
            )
        except A2ATransportFailure:
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "A2A 没有取得固定配置的可信卡片"
            ) from None
        try:
            value = decode(reply.payload)
            check_a2a_content(value, self._secret)
            if reply.status != 200:
                raise ValueError()
            return A2ABinding(server=self.server, card_json=encode(value).decode("utf-8"))
        except (AgentGitError, ValueError):
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "A2A 没有取得固定配置的可信卡片"
            ) from None

    def exchange(self, binding: A2ABinding, call: A2ACall) -> bytes:
        binding = parse_contract(encode(binding.model_dump(mode="json")), A2ABinding)
        call = parse_contract(encode(call.model_dump(mode="json")), A2ACall)
        if binding.server != self.server:
            raise AgentGitError(ErrorCode.CONFIGURATION, "A2A 客户端与固定端点／主体不符")
        try:
            call.validate_binding(binding)
            check_a2a_content(binding.model_dump(mode="json"), self._secret)
            check_a2a_content(call.model_dump(mode="json"), self._secret)
            check_a2a_content(call.body(), self._secret)
        except ValueError:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 请求绑定或内容不符合本地权限") from None
        try:
            reply = request(
                self.server,
                call.operation,
                b"" if call.operation == "get" else call.body_json.encode("utf-8"),
                self._secret,
                task_id=call.task.task_id if call.task and call.operation != "send" else None,
                deadline=self._deadline,
                guard=self._guard,
            )
        except A2ATransportFailure as exc:
            raise _failed(call.operation, sent=exc.sent) from None
        try:
            value = decode(reply.payload)
            check_a2a_content(value, self._secret)
        except (AgentGitError, ValueError):
            raise _failed(call.operation, sent=True) from None
        if reply.status != 200:
            raise _rejection(reply.status, value) or _failed(call.operation, sent=True)
        try:
            if call.operation == "send":
                if not isinstance(value, dict) or set(value) != {"task"}:
                    raise ValueError()
                value = value["task"]
            task(value)
            if call.task is not None and (
                value["id"] != call.task.task_id or value["contextId"] != call.task.context_id
            ):
                raise ValueError()
            result = A2AResult(
                operation=call.operation,
                binding_ref=binding_digest(binding),
                request_ref=object_digest(ObjectType.BLOB, encode(call.model_dump(mode="json"))),
                task_json=encode(value).decode("utf-8"),
            )
            payload = encode(result.model_dump(mode="json"))
            if len(payload) > self.server.max_response_bytes:
                raise ValueError()
            return payload
        except (ValueError, AgentGitError):
            raise _failed(call.operation, sent=True) from None


class A2AAdapter:
    def __init__(self, binding: A2ABinding, client: A2AClient | None = None):
        self.binding = parse_contract(encode(binding.model_dump(mode="json")), A2ABinding)
        if client is not None and client.server != self.binding.server:
            raise AgentGitError(ErrorCode.CONFIGURATION, "A2A 客户端与绑定不符")
        self.client = client

    def call(self, gateway: CallGateway, *, step: str, attempt: int, call: A2ACall) -> CallOutcome:
        request_bytes = encode(call.model_dump(mode="json"))
        checked = parse_contract(request_bytes, A2ACall)
        try:
            checked.validate_binding(self.binding)
            if self.client is not None:
                check_a2a_content(self.binding.model_dump(mode="json"), self.client._secret)
                check_a2a_content(checked.model_dump(mode="json"), self.client._secret)
                check_a2a_content(checked.body(), self.client._secret)
        except ValueError:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 请求不符合固定绑定或内容权限") from None
        outcome = gateway.call(
            step=step,
            attempt=attempt,
            provider=self.binding.provider(checked.operation),
            signature=self.binding.signature(checked.operation),
            effect=self.binding.effect(checked.operation),
            request=request_bytes,
            invoke=lambda: self.invoke(request_bytes),
            max_bytes=self.binding.server.max_response_bytes,
        )
        if outcome.error is None:
            result = parse_contract(outcome.payload, A2AResult)
            if (
                result.binding_ref != binding_digest(self.binding)
                or result.operation != checked.operation
                or result.request_ref != object_digest(ObjectType.BLOB, request_bytes)
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "A2A 结果与固定请求／绑定不符")
            if checked.task is not None:
                value = result.value()
                if (
                    value["id"] != checked.task.task_id
                    or value["contextId"] != checked.task.context_id
                ):
                    raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "A2A 结果与确切任务不符")
        return outcome

    def invoke(self, payload: bytes) -> bytes:
        if self.client is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A Recorded 路径没有 Live 客户端")
        return self.client.exchange(self.binding, parse_contract(payload, A2ACall))
