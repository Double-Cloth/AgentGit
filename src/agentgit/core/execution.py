"""S4 断网执行的冻结请求、文件清单与可重放结果。"""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.models import Contract, Digest
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.snapshots import ArtifactEntry

SANDBOX_PROVIDERS = frozenset({"tool:terminal_exec/v1", "tool:code_exec/v1"})


class ExecutionPolicy(Contract):
    version: Literal["sandbox-v1"] = "sandbox-v1"
    image_id: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    packages_digest: Digest
    interpreter: Literal["/usr/local/bin/python"] = "/usr/local/bin/python"
    interpreter_version: Literal["3.12.14"] = "3.12.14"
    shell: Literal["/bin/sh"] = "/bin/sh"
    shell_version: Literal["dash:0.5.12-2"] = "dash:0.5.12-2"
    network: Literal["none"] = "none"
    cpu_millicores: Literal[1000] = 1000
    memory_bytes: Literal[536870912] = 536870912
    pids: Literal[64] = 64
    workspace_bytes: Literal[134217728] = 134217728
    temporary_bytes: Literal[33554432] = 33554432
    max_timeout_ms: Literal[120000] = 120000
    default_timeout_ms: Literal[30000] = 30000
    max_file_bytes: Literal[8388608] = 8388608
    max_manifest_bytes: Literal[33554432] = 33554432
    max_files: Literal[1000] = 1000
    max_log_bytes: Literal[1048576] = 1048576
    result_envelope_bytes: Literal[8192] = 8192

    def digest(self) -> str:
        return object_digest(ObjectType.BLOB, encode(self.model_dump(mode="json")))


class ExecutionFile(ArtifactEntry):
    byte_length: Annotated[int, Field(ge=0, le=8388608)]


class ExecutionManifest(Contract):
    files: Annotated[tuple[ExecutionFile, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def valid_files(self) -> "ExecutionManifest":
        validate_artifact_paths(item.path for item in self.files)
        if sum(item.byte_length for item in self.files) > 33554432:
            raise ValueError("执行文件总量超过 32 MiB")
        if tuple(sorted(self.files, key=lambda item: item.path)) != self.files:
            raise ValueError("执行文件清单必须按路径排序")
        return self


class ExecutionArguments(Contract):
    cwd: Annotated[str, Field(min_length=1, max_length=240)] = "."
    stdin_ref: Digest | None = None
    input_manifest_ref: Digest
    timeout_ms: Annotated[int, Field(ge=1, le=120000)] = 30000
    execution_policy_ref: Digest

    @field_validator("cwd")
    @classmethod
    def relative_cwd(cls, value: str) -> str:
        if value != ".":
            validate_artifact_paths([value])
        return value


class TerminalArguments(ExecutionArguments):
    command_ref: Digest


class CodeArguments(ExecutionArguments):
    language: Annotated[str, Field(min_length=1, max_length=30)] = "python"
    code_ref: Digest | None = None
    script_path: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    argv: Annotated[tuple[Annotated[str, Field(max_length=4096)], ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def code_or_script(self) -> "CodeArguments":
        if (self.code_ref is None) == (self.script_path is None):
            raise ValueError("code_ref 与 script_path 必须且只能提供一个")
        if self.script_path is not None:
            validate_artifact_paths([self.script_path])
        if any("\0" in item for item in self.argv) or sum(map(len, self.argv)) > 16384:
            raise ValueError("argv 含 NUL 或超过总长度限制")
        return self


ExecutionFailure = Literal[
    "execution_failed",
    "execution_timeout",
    "execution_cancelled",
    "resource_limit",
    "output_limit",
    "export_invalid",
    "sandbox_unavailable",
    "unsupported_language",
    "outcome_unknown",
]


class ExecutionResult(Contract):
    exit_code: Annotated[int, Field(ge=0, le=255)] | None
    error_code: ExecutionFailure | None
    stdout_ref: Digest
    stderr_ref: Digest
    stdout_truncated: bool
    stderr_truncated: bool
    input_manifest_ref: Digest
    output_manifest_ref: Digest | None
    command_ref: Digest | None = None
    code_ref: Digest | None = None
    script_path: str | None = None
    image_id: str
    interpreter: str
    interpreter_version: str
    execution_policy_ref: Digest
    cleanup_confirmed: bool
    integrity_complete: bool

    @model_validator(mode="after")
    def safe_result(self) -> "ExecutionResult":
        if not self.cleanup_confirmed and self.error_code != "outcome_unknown":
            raise ValueError("未确认清理的执行只能记录为结果未知")
        if self.error_code is None and (
            self.exit_code != 0
            or not self.cleanup_confirmed
            or not self.integrity_complete
            or self.output_manifest_ref is None
        ):
            raise ValueError("成功执行必须有完整结果、清理确认和输出清单")
        if self.error_code is not None and self.output_manifest_ref is not None:
            raise ValueError("失败执行不能发布输出工作区")
        return self
