"""A2A 本机配置的不可变绑定；不建立网络连接或扩大远端权限。"""

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.effects import ToolRule
from agentgit.core.models import Contract, EffectClass

A2A_VERSION: Literal["1.0"] = "1.0"
A2A_RELEASE: Literal["v1.0.1"] = "v1.0.1"
A2AName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
A2AOperation = Literal["discover", "send", "get", "cancel"]


def _fields(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ValueError("A2A 对象字段超出声明范围")
    return value


def _text(value: Any, limit: int = 1000) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError("A2A 文本为空或超过上限")


def check_a2a_content(value: Any, secret: str | None = None) -> None:
    private = {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "password",
        "secret",
        "credentials",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                not isinstance(key, str)
                or key.lower().replace("-", "_") in private
                or (secret is not None and secret in key)
            ):
                raise ValueError("A2A 内容不能携带凭据")
            check_a2a_content(child, secret)
    elif isinstance(value, list):
        for child in value:
            check_a2a_content(child, secret)
    elif isinstance(value, str) and secret is not None and secret in value:
        raise ValueError("A2A 内容不能回显运行时凭据")


class A2AServer(Contract):
    server_identity: A2AName
    principal_id: A2AName
    endpoint: str
    protocol_version: Literal["1.0"] = A2A_VERSION
    upstream_release: Literal["v1.0.1"] = A2A_RELEASE
    timeout_ms: Annotated[int, Field(ge=20, le=30_000)] = 2000
    max_response_bytes: Annotated[int, Field(ge=128, le=1_048_576)] = 65_536

    @property
    def credential_env(self) -> str:
        server = self.server_identity.upper().replace("-", "_")
        principal = self.principal_id.upper().replace("-", "_")
        return f"AGENTGIT_A2A_{server}_{principal}_TOKEN"

    @model_validator(mode="after")
    def registered_loopback(self) -> "A2AServer":
        try:
            port = urlsplit(self.endpoint).port
        except ValueError:
            raise ValueError("A2A 登记地址无效") from None
        if port is None or not 1 <= port <= 65535 or self.endpoint != f"http://127.0.0.1:{port}":
            raise ValueError("A2A 仅支持显式登记的回环 HTTP 根地址")
        return self


def validate_card(value: Any, server: A2AServer) -> None:
    card = _fields(
        value,
        {
            "name",
            "description",
            "version",
            "supportedInterfaces",
            "capabilities",
            "securitySchemes",
            "securityRequirements",
            "defaultInputModes",
            "defaultOutputModes",
            "skills",
        },
    )
    check_a2a_content(card)
    for name in ("name", "description", "version"):
        _text(card[name])
    if card["supportedInterfaces"] != [
        {"url": server.endpoint, "protocolBinding": "HTTP+JSON", "protocolVersion": A2A_VERSION}
    ]:
        raise ValueError("A2A 卡片不能改写登记地址、绑定或版本")
    capabilities = _fields(
        card["capabilities"], {"streaming", "pushNotifications", "extendedAgentCard"}
    )
    if any(value is not False for value in capabilities.values()):
        raise ValueError("A2A 本地配置不支持流式、推送或扩展卡片")
    if card["securitySchemes"] != {
        "fixtureBearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
    }:
        raise ValueError("A2A 本地配置仅支持固定 Bearer 认证声明")
    if card["securityRequirements"] != [{"schemes": {"fixtureBearer": {"list": []}}}]:
        raise ValueError("A2A 认证要求与本地配置不符")
    if card["defaultInputModes"] != ["text/plain"] or card["defaultOutputModes"] != [
        "text/plain",
        "application/json",
    ]:
        raise ValueError("A2A 卡片媒体类型超出本地配置")
    skills = card["skills"]
    if not isinstance(skills, list) or len(skills) != 1:
        raise ValueError("A2A 仅支持固定公开资料整理能力")
    skill = _fields(skills[0], {"id", "name", "description", "tags", "inputModes", "outputModes"})
    if skill["id"] != "public-research-summary":
        raise ValueError("A2A 未登记的远端能力")
    for name in ("name", "description"):
        _text(skill[name])
    if not isinstance(skill["tags"], list) or not 1 <= len(skill["tags"]) <= 8:
        raise ValueError("A2A 能力标签无效")
    for tag in skill["tags"]:
        _text(tag, 100)
    if skill["inputModes"] != ["text/plain"] or skill["outputModes"] != [
        "text/plain",
        "application/json",
    ]:
        raise ValueError("A2A 能力媒体类型超出本地配置")


class A2ABinding(Contract):
    server: A2AServer
    card_json: Annotated[str, Field(max_length=65_536)]
    profile: Literal["public-research-summary/v1"] = "public-research-summary/v1"

    @model_validator(mode="after")
    def fixed_card(self) -> "A2ABinding":
        validate_card(self.card(), self.server)
        return self

    def card(self) -> dict[str, Any]:
        value = decode(self.card_json.encode("utf-8"))
        if not isinstance(value, dict) or encode(value).decode("utf-8") != self.card_json:
            raise ValueError("A2A 卡片必须为 canonical object")
        return value

    def signature(self, operation: A2AOperation) -> str:
        self.effect(operation)
        return object_digest(
            ObjectType.CALL,
            encode({"binding": self.model_dump(mode="json"), "operation": operation}),
        )

    def provider(self, operation: A2AOperation) -> str:
        self.effect(operation)
        return f"a2a:{self.server.server_identity}:{self.server.principal_id}:{operation}/v1"

    @staticmethod
    def effect(operation: A2AOperation) -> EffectClass:
        if operation in ("discover", "get"):
            return EffectClass.READ_ONLY
        if operation == "send":
            return EffectClass.NON_IDEMPOTENT_WRITE
        if operation == "cancel":
            return EffectClass.IDEMPOTENT_WRITE
        raise ValueError("A2A 操作超出声明范围")

    def rule(self, operation: A2AOperation) -> ToolRule:
        effect = self.effect(operation)
        return ToolRule(
            provider=self.provider(operation),
            signature_digest=self.signature(operation),
            effect=effect,
            scope="external",
            preauthorized=effect == EffectClass.READ_ONLY,
        )
