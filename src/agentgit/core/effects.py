"""由本地注册表决定的工具策略与精确动作绑定。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallOccurrence, Contract, Digest, EffectClass, Identifier


class ToolRule(Contract):
    provider: Annotated[str, Field(min_length=1, max_length=200)]
    signature_digest: Digest
    effect: EffectClass
    scope: Literal["local", "sandbox", "external"]
    preauthorized: bool = False

    @model_validator(mode="after")
    def allowed_preauthorization(self) -> "ToolRule":
        if self.preauthorized and (
            self.effect == EffectClass.IRREVERSIBLE
            or (self.scope == "external" and self.effect != EffectClass.READ_ONLY)
            or (self.scope == "local" and self.effect == EffectClass.NON_IDEMPOTENT_WRITE)
        ):
            raise ValueError("该效果必须精确审批，不能预授权")
        return self


class ToolPolicy(Contract):
    version: Annotated[int, Field(ge=1)] = 1
    rules: Annotated[tuple[ToolRule, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def unique_providers(self) -> "ToolPolicy":
        if len({rule.provider for rule in self.rules}) != len(self.rules):
            raise ValueError("同一策略不能重复注册工具")
        return self

    def digest(self) -> str:
        return object_digest(ObjectType.BLOB, encode(self.model_dump(mode="json")))

    def rule(self, occurrence: CallOccurrence, effect: EffectClass) -> ToolRule:
        for rule in self.rules:
            if rule.provider == occurrence.provider:
                if rule.signature_digest != occurrence.signature_digest or rule.effect != effect:
                    raise AgentGitError(ErrorCode.FORBIDDEN, "调用签名或效果分类与本地策略不符")
                return rule
        raise AgentGitError(ErrorCode.FORBIDDEN, "工具未在本地策略中登记")


class BoundAction(Contract):
    repository_id: Identifier
    run_id: Identifier
    branch_name: str
    checkpoint: Digest | None
    occurrence: CallOccurrence
    effect: EffectClass
    policy_digest: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    max_response_bytes: Annotated[int, Field(ge=1, le=8 * 1024 * 1024)]

    def digest(self) -> str:
        return object_digest(ObjectType.CALL, encode(self.model_dump(mode="json")))
