"""Runtime 使用的调用和持久化边界，不包含数据库实现。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agentgit.core.errors import AgentGitError
from agentgit.core.models import EffectClass, ModelCallBudget
from agentgit.core.research import AgentState, CallTrace


@dataclass(frozen=True)
class CallOutcome:
    payload: bytes
    trace: CallTrace
    error: AgentGitError | None = None
    output_bytes: int = 0


@dataclass(frozen=True)
class VerifiedOutcome:
    payload: bytes
    evidence: bytes


class OutcomeResolver(Protocol):
    provider: str
    signature: str

    def resolve(self, request: bytes) -> VerifiedOutcome | None: ...


@dataclass(frozen=True)
class CallSettlement:
    before: AgentState
    request: bytes
    outcome: CallOutcome


class ModelAdapter(Protocol):
    signature: str

    def invoke(self, payload: bytes) -> bytes: ...


class BoundedModelAdapter(ModelAdapter, Protocol):
    def invoke_bounded(
        self,
        payload: bytes,
        *,
        timeout_ms: int,
        max_reply_bytes: int,
        cost_limit_micro_usd: int,
    ) -> bytes: ...


class CallGateway(Protocol):
    def recorded(
        self,
        *,
        step: str,
        attempt: int,
        provider: str,
        signature: str,
        effect: EffectClass,
        request: bytes,
        max_bytes: int,
        model_budget: ModelCallBudget | None = None,
    ) -> CallOutcome | None: ...

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
        model_budget: ModelCallBudget | None = None,
    ) -> CallOutcome: ...


class StateJournal(Protocol):
    def save(self, state: AgentState, event_type: str, details: dict[str, Any]) -> None: ...
