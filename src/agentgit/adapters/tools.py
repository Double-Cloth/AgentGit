"""本地 Tool Registry 的参数、权限和结果校验。"""

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.workspace import Workspace
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import (
    CodeArguments,
    ExecutionContext,
    ExecutionResult,
    TerminalArguments,
    execution_signature,
)
from agentgit.core.models import Contract, EffectClass, parse_contract
from agentgit.core.research import AgentConfig, CallTrace


class SearchArguments(Contract):
    frameworks: tuple[str, ...]


class ReadArguments(Contract):
    source_id: Annotated[str, Field(min_length=1, max_length=100)]


class WriteArguments(Contract):
    path: str
    text: str
    media_type: str


class ToolRegistry:
    def __init__(self, corpus: Corpus, workspace: Workspace, config: AgentConfig):
        self.corpus, self.workspace, self.allowed = corpus, workspace, frozenset(config.tools)
        self.executions: dict[str, int] = {}
        self.policy = config.execution_policy
        self.input_context: Callable[[], ExecutionContext] | None = None
        self.execution_files: (
            Callable[[ExecutionResult, CallTrace], tuple[dict[str, bytes], dict[str, str]]] | None
        ) = None

    def context(self) -> ExecutionContext | None:
        if self.policy is None:
            return None
        if self.input_context is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "容器工具缺少受租约保护的输入上下文")
        return self.input_context()

    def prepare(self, name: str, arguments: dict[str, Any]) -> None:
        self.effect(name)
        if name in {"terminal_exec", "code_exec"}:
            contract = TerminalArguments if name == "terminal_exec" else CodeArguments
            request = parse_contract(encode(arguments), contract)
            context = self.context()
            if (
                context is None
                or request.input_manifest_ref != context.input_manifest_ref
                or request.execution_policy_ref != context.execution_policy_ref
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "执行请求与当前工作区或策略不符")

    def effect(self, name: str) -> EffectClass:
        if name not in self.allowed:
            raise AgentGitError(ErrorCode.FORBIDDEN, "工具未被当前配置授权")
        if name in {"terminal_exec", "code_exec"}:
            return EffectClass.NON_IDEMPOTENT_WRITE
        return EffectClass.IDEMPOTENT_WRITE if name == "artifact_write" else EffectClass.READ_ONLY

    def signature(self, name: str) -> str:
        effect = self.effect(name)
        if name in {"terminal_exec", "code_exec"}:
            if self.policy is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "容器工具缺少固定策略")
            return execution_signature(f"tool:{name}/v1", self.policy)
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

    def observe_execution(
        self, name: str, arguments: dict[str, Any], payload: bytes, trace: CallTrace
    ) -> None:
        policy = self.policy
        if policy is None or self.execution_files is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行观察缺少固定策略或录制读取器")
        schema = TerminalArguments if name == "terminal_exec" else CodeArguments
        request = parse_contract(encode(arguments), schema)
        result = parse_contract(payload, ExecutionResult)
        expected = {
            "input_manifest_ref": request.input_manifest_ref,
            "execution_policy_ref": policy.digest(),
            "image_id": policy.image_id,
            "command_ref": request.command_ref if isinstance(request, TerminalArguments) else None,
            "code_ref": request.code_ref if isinstance(request, CodeArguments) else None,
            "script_path": request.script_path if isinstance(request, CodeArguments) else None,
            "interpreter": policy.shell if name == "terminal_exec" else policy.interpreter,
            "interpreter_version": policy.shell_version
            if name == "terminal_exec"
            else policy.interpreter_version,
        }
        if any(getattr(result, key) != value for key, value in expected.items()):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "执行录制与原参数、镜像或解释器不符")
        if (
            result.error_code is not None
            or not result.integrity_complete
            or not result.cleanup_confirmed
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "不能把失败或不完整执行作为成功观察")
        files, media = self.execution_files(result, trace)
        self.workspace.replace(files, media)
