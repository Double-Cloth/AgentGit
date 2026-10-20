"""本地 Tool Registry 的参数、权限和结果校验。"""

from typing import Annotated, Any

from pydantic import Field

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, EffectClass, parse_contract
from agentgit.core.research import AgentConfig


class SearchArguments(Contract):
    frameworks: tuple[str, ...]


class ReadArguments(Contract):
    source_id: Annotated[str, Field(min_length=1, max_length=100)]


class WriteArguments(Contract):
    path: str
    text: str
    media_type: str


class ToolRegistry:
    def __init__(self, corpus: Corpus, workspace: ArtifactWorkspace, config: AgentConfig):
        self.corpus, self.workspace, self.allowed = corpus, workspace, frozenset(config.tools)
        self.executions: dict[str, int] = {}

    def effect(self, name: str) -> EffectClass:
        if name not in self.allowed:
            raise AgentGitError(ErrorCode.FORBIDDEN, "工具未被当前配置授权")
        return EffectClass.IDEMPOTENT_WRITE if name == "artifact_write" else EffectClass.READ_ONLY

    def signature(self, name: str) -> str:
        effect = self.effect(name)
        schemas: dict[str, type[Contract]] = {
            "corpus_search": SearchArguments,
            "source_read": ReadArguments,
            "artifact_write": WriteArguments,
        }
        return object_digest(
            ObjectType.CALL,
            encode(
                {
                    "schema_version": 1,
                    "tool": name,
                    "version": "v1",
                    "effect": effect.value,
                    "arguments": schemas[name].model_json_schema(),
                }
            ),
        )

    def invoke(self, name: str, arguments: dict[str, Any]) -> bytes:
        self.effect(name)
        payload = encode(arguments)
        if name == "corpus_search":
            request = parse_contract(payload, SearchArguments)
            if not set(request.frameworks) <= {
                source.framework for source in self.corpus.sources.values()
            }:
                raise AgentGitError(ErrorCode.VALIDATION, "搜索仅支持固定合成框架")
            result = {
                "schema_version": 1,
                "source_ids": list(self.corpus.search(request.frameworks)),
            }
        elif name == "source_read":
            read = parse_contract(payload, ReadArguments)
            result = {
                "schema_version": 1,
                "source": self.corpus.read(read.source_id).model_dump(mode="json"),
            }
        elif name == "artifact_write":
            write = parse_contract(payload, WriteArguments)
            self.workspace.write(write.path, write.text, write.media_type)
            result = {
                "schema_version": 1,
                "path": write.path,
                "content_ref": object_digest(ObjectType.BLOB, write.text.encode("utf-8")),
            }
        else:
            raise AgentGitError(ErrorCode.FORBIDDEN, "未知工具")
        self.executions[name] = self.executions.get(name, 0) + 1
        return encode(result)
