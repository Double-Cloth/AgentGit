"""MCP 本地登记与不可变工具绑定；不包含凭据或网络客户端。"""

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.effects import ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp_schema import check_schema
from agentgit.core.models import Contract, EffectClass

MCP_VERSION: Literal["2026-07-28"] = "2026-07-28"
MCPName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


def check_mcp_credentials(value: Any, secret: str | None = None) -> None:
    private = {"authorization", "api_key", "apikey", "access_token", "password", "secret"}
    if isinstance(value, dict):
        if any(
            key.lower().replace("-", "_") in private or (secret is not None and secret in key)
            for key in value
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "MCP 业务内容不能携带凭据字段")
        for child in value.values():
            check_mcp_credentials(child, secret)
    elif isinstance(value, list):
        for child in value:
            check_mcp_credentials(child, secret)
    elif isinstance(value, str) and secret is not None and secret in value:
        raise AgentGitError(ErrorCode.FORBIDDEN, "MCP 业务内容不能包含运行时凭据")


class MCPServer(Contract):
    server_identity: MCPName
    endpoint: str
    protocol_version: Literal["2026-07-28"] = MCP_VERSION
    timeout_ms: Annotated[int, Field(ge=20, le=30_000)] = 2000
    max_response_bytes: Annotated[int, Field(ge=128, le=1_048_576)] = 65_536

    @model_validator(mode="after")
    def registered_loopback(self) -> "MCPServer":
        try:
            parts = urlsplit(self.endpoint)
            port = parts.port
        except ValueError:
            raise ValueError("MCP 端点格式无效") from None
        if (
            port is None
            or not 1 <= port <= 65535
            or self.endpoint != f"http://127.0.0.1:{port}/mcp"
        ):
            raise ValueError("MCP 仅支持显式登记的回环 HTTP /mcp 端点")
        return self


class MCPBinding(Contract):
    server: MCPServer
    tool_name: MCPName
    # 保存 canonical 文本，避免冻结模型内部可变 dict 改写固定 Schema。
    definition_json: Annotated[str, Field(max_length=131_072)]
    effect_class: EffectClass

    @model_validator(mode="after")
    def fixed_definition(self) -> "MCPBinding":
        definition = self.definition()
        if definition.get("name") != self.tool_name:
            raise ValueError("MCP 工具名与固定定义不符")
        schema = definition.get("inputSchema")
        check_schema(schema)
        if not isinstance(schema, dict) or schema["type"] != "object":
            raise ValueError("MCP 参数 Schema 必须为 object")
        if "outputSchema" in definition:
            check_schema(definition["outputSchema"])
        return self

    def definition(self) -> dict[str, Any]:
        value = decode(self.definition_json.encode("utf-8"))
        if not isinstance(value, dict) or encode(value).decode("utf-8") != self.definition_json:
            raise ValueError("MCP 定义必须为 canonical object")
        return value

    def schema_digest(self) -> str:
        return object_digest(ObjectType.BLOB, self.definition_json.encode("utf-8"))

    def signature(self) -> str:
        return object_digest(ObjectType.CALL, encode(self.model_dump(mode="json")))

    @property
    def provider(self) -> str:
        return f"mcp:{self.server.server_identity}:{self.tool_name}/v1"

    def rule(self) -> ToolRule:
        return ToolRule(
            provider=self.provider,
            signature_digest=self.signature(),
            effect=self.effect_class,
            scope="external",
            preauthorized=self.effect_class == EffectClass.READ_ONLY,
        )


class MCPArguments(Contract):
    arguments: dict[str, Any]
