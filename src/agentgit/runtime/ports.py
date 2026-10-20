"""Runtime 使用的调用和持久化边界，不包含数据库实现。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agentgit.core.errors import AgentGitError
from agentgit.core.models import EffectClass
from agentgit.core.research import AgentState, CallTrace


@dataclass(frozen=True)
class CallOutcome:
    payload: bytes
    trace: CallTrace
    error: AgentGitError | None = None
    output_bytes: int = 0


class ModelAdapter(Protocol):
    signature: str

    def invoke(self, payload: bytes) -> bytes: ...


class CallGateway(Protocol):
    def call(
        self,
        *,
        step: str,
        attempt: int,
        provider: str,
        signature: str,
        effect: EffectClass,
        request: bytes,
        invoke: Callable[[], bytes],
        max_bytes: int,
    ) -> CallOutcome: ...


class StateJournal(Protocol):
    def save(self, state: AgentState, event_type: str, details: dict[str, Any]) -> None: ...
