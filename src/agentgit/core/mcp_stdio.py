"""后端操作者对白名单单文件 Python Server 的固定批准记录。"""

from typing import Annotated, Literal

from pydantic import Field

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.models import Contract, Digest


class MCPApprovedProgram(Contract):
    kind: Literal["mcp_approved_python/v1"] = "mcp_approved_python/v1"
    interpreter: Annotated[str, Field(min_length=1, max_length=4096)]
    interpreter_sha256: Digest
    script: Annotated[str, Field(min_length=1, max_length=4096)]
    script_sha256: Digest
    approved_by: Annotated[str, Field(min_length=1, max_length=200)]

    def reference(self) -> str:
        return object_digest(ObjectType.BLOB, encode(self.model_dump(mode="json")))


class MCPStdioRegistry(Contract):
    programs: Annotated[tuple[MCPApprovedProgram, ...], Field(min_length=1, max_length=16)]
