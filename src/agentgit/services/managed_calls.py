"""受管理动作在调用意图/结果事务中的扩展点；不允许在事务内执行动作。"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.engine import Connection

from agentgit.core.errors import AgentGitError
from agentgit.core.models import CallOccurrence
from agentgit.storage.objects import StoredObject


@dataclass(frozen=True)
class ManagedCallContext:
    repository_id: UUID
    run_id: UUID
    call_id: UUID
    occurrence: CallOccurrence
    checkpoint: str | None
    request: bytes
    intent_ref: str
    max_bytes: int


@dataclass(frozen=True)
class ManagedCallResult:
    payload: bytes
    error: AgentGitError | None = None


class ManagedCalls(Protocol):
    def stage(self, context: ManagedCallContext) -> None: ...

    def prepare(self, connection: Connection, context: ManagedCallContext) -> None: ...

    def invoke(self, context: ManagedCallContext) -> ManagedCallResult: ...

    def record(
        self,
        connection: Connection,
        context: ManagedCallContext,
        response: StoredObject,
        result: StoredObject,
    ) -> None: ...
