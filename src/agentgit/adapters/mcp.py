"""可信本地 MCP 工具；Live 经过原调用网关，Recorded 不触网。"""

import os
import re
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from agentgit.adapters.mcp_http import MCPTransportFailure, post
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import (
    MCP_VERSION,
    MCPArguments,
    MCPBinding,
    MCPServer,
    check_mcp_credentials,
)
from agentgit.core.mcp_schema import validate_value
from agentgit.core.models import EffectClass, parse_contract
from agentgit.runtime.ports import CallGateway, CallOutcome


def configured_client(server: MCPServer) -> "MCPClient":
    name = "AGENTGIT_MCP_" + server.server_identity.upper().replace("-", "_") + "_TOKEN"
    return MCPClient(server, credential_env=name if name in os.environ else None)


class MCPClient:
    def __init__(self, server: MCPServer, *, credential_env: str | None = None):
        self.server = server
        self._deadline: float | None = None
        self._request_guard: Callable[[], None] | None = None
        self._secret: str | None = None
        if credential_env is not None:
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", credential_env) is None:
                raise AgentGitError(ErrorCode.CONFIGURATION, "MCP 凭据环境变量名称无效")
            secret = os.environ.get(credential_env)
            if secret is None or re.fullmatch(r"[\x21-\x7e]{1,4096}", secret) is None:
                raise AgentGitError(ErrorCode.CONFIGURATION, "MCP 运行时凭据缺失或无效")
            self._secret = secret

    def restrict_deadline(self, remaining_ms: int) -> None:
        if type(remaining_ms) is not int or not 1 <= remaining_ms <= 900_000:
            raise AgentGitError(ErrorCode.VALIDATION, "MCP 实际剩余期限无效")
        deadline = time.monotonic() + remaining_ms / 1000
        self._deadline = deadline if self._deadline is None else min(self._deadline, deadline)

    def guard_requests(self, guard: Callable[[], None]) -> None:
        self._request_guard = guard

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid4())
        payload = encode(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {
                    **params,
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "AgentGit",
                            "version": "0.1.0",
                        },
                    },
                },
            }
        )
        if self._request_guard is not None:
            self._request_guard()
        reply = post(
            self.server, method, params.get("name"), payload, self._secret, deadline=self._deadline
        )
        # 从未反射 HTTP 正文或远端错误文本，避免凭据回显进入异常和历史。
        result = decode(reply.payload)
        if (
            reply.status != 200
            or not isinstance(result, dict)
            or result.get("jsonrpc") != "2.0"
            or result.get("id") != request_id
            or "error" in result
            or not isinstance(result.get("result"), dict)
        ):
            raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 协议响应不符合声明范围")
        value: dict[str, Any] = result["result"]
        if value.get("resultType") != "complete":
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "MCP 仅支持完整工具响应")
        check_mcp_credentials(value, self._secret)
        return value

    def list_tools(self) -> tuple[bytes, ...]:
        definitions: dict[str, bytes] = {}
        cursor: str | None = None
        seen: set[str] = set()
        try:
            for _ in range(16):
                result = self._rpc("tools/list", {"cursor": cursor} if cursor is not None else {})
                tools = result.get("tools")
                if not isinstance(tools, list) or len(tools) > 128:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 工具列表超过上限")
                for tool in tools:
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                        raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 工具定义无效")
                    name = tool["name"]
                    if name in definitions or len(definitions) >= 128:
                        raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 工具名重复或超过上限")
                    definitions[name] = encode(tool)
                cursor = result.get("nextCursor")
                if cursor is None:
                    return tuple(definitions[key] for key in sorted(definitions))
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1000 or cursor in seen:
                    raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 分页游标无效")
                seen.add(cursor)
        except MCPTransportFailure:
            raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 只读发现未取得完整响应") from None
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 工具分页超过上限")

    def bind(self, tool_name: str, effect: EffectClass) -> MCPBinding:
        for raw in self.list_tools():
            definition = decode(raw)
            if definition["name"] == tool_name:
                return MCPBinding(
                    server=self.server,
                    tool_name=tool_name,
                    definition_json=raw.decode("utf-8"),
                    effect_class=effect,
                )
        raise AgentGitError(ErrorCode.NOT_FOUND, "MCP 未列出指定工具")


class MCPAdapter:
    def __init__(self, binding: MCPBinding, client: MCPClient | None = None):
        if client is not None and client.server != binding.server:
            raise AgentGitError(ErrorCode.CONFIGURATION, "MCP 客户端与固定端点不符")
        self.binding, self.client = binding, client

    def call(
        self, gateway: CallGateway, *, step: str, attempt: int, arguments: dict[str, Any]
    ) -> CallOutcome:
        if len(encode(arguments)) > 1_048_000:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 参数超过请求上限")
        check_mcp_credentials(arguments, self.client._secret if self.client else None)
        validate_value(self.binding.definition()["inputSchema"], arguments)
        request = encode(MCPArguments(arguments=arguments).model_dump(mode="json"))
        return gateway.call(
            step=step,
            attempt=attempt,
            provider=self.binding.provider,
            signature=self.binding.signature(),
            effect=self.binding.effect_class,
            request=request,
            invoke=lambda: self.invoke(request),
            max_bytes=self.binding.server.max_response_bytes,
        )

    def invoke(self, request: bytes) -> bytes:
        arguments = parse_contract(request, MCPArguments).arguments
        if len(request) > 1_048_000:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 参数超过请求上限")
        validate_value(self.binding.definition()["inputSchema"], arguments)
        check_mcp_credentials(arguments, self.client._secret if self.client else None)
        return self._invoke(request)

    def _invoke(self, request: bytes) -> bytes:
        client, binding = self.client, self.binding
        if client is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "MCP Recorded 路径没有 Live 客户端")
        definitions = client.list_tools()
        if binding.definition_json.encode("utf-8") not in definitions:
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "MCP 工具定义已变化，必须重新绑定和审批"
            )
        arguments = parse_contract(request, MCPArguments).arguments
        started = time.monotonic_ns()
        try:
            result = client._rpc("tools/call", {"name": binding.tool_name, "arguments": arguments})
            content = result.get("content")
            if (
                not isinstance(content, list)
                or len(content) > 128
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"type", "text"}
                    or item["type"] != "text"
                    or not isinstance(item["text"], str)
                    for item in content
                )
                or type(result.get("isError", False)) is not bool
                or result.get("isError", False)
            ):
                raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 工具没有返回受支持的成功内容")
            definition = binding.definition()
            if "outputSchema" in definition:
                if "structuredContent" not in result:
                    raise AgentGitError(ErrorCode.VALIDATION, "MCP 缺少声明的结构化输出")
                validate_value(definition["outputSchema"], result["structuredContent"])
            normalized = {
                "schema_version": 1,
                "kind": "mcp_tool_result/v1",
                "server_identity": binding.server.server_identity,
                "tool_name": binding.tool_name,
                "schema_digest": binding.schema_digest(),
                "arguments_digest": object_digest(ObjectType.BLOB, request),
                "latency_ms": (time.monotonic_ns() - started) // 1_000_000,
                "effect_class": binding.effect_class.value,
                "cancellation": "transport_only",
                "content": content,
            }
            if "structuredContent" in result:
                normalized["structuredContent"] = result["structuredContent"]
            payload = encode(normalized)
            if len(payload) > binding.server.max_response_bytes:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 标准化结果超过上限")
            return payload
        except (MCPTransportFailure, AgentGitError) as exc:
            if binding.effect_class != EffectClass.READ_ONLY and (
                not isinstance(exc, MCPTransportFailure) or exc.sent
            ):
                raise AgentGitError(
                    ErrorCode.OUTCOME_UNKNOWN, "MCP 写请求未取得可信成功回执，不得自动重发"
                ) from None
            raise AgentGitError(ErrorCode.TOOL_ERROR, "MCP 调用未取得受支持的完整结果") from None
