"""S2 完整状态封装；S3 的 Agent 状态位于 data 内。"""

from typing import Any

from agentgit.core.models import Contract, Digest, NonNegative


class ArtifactEntry(Contract):
    path: str
    object_ref: Digest
    media_type: str


class StateSnapshot(Contract):
    data: dict[str, Any]
    artifacts: tuple[ArtifactEntry, ...] = ()


class LogicalEvent(Contract):
    seq: NonNegative
    type: str
    logical_step_id: str
    payload_ref: Digest
    cause: dict[str, str | int] | None = None


class EventManifest(Contract):
    events: tuple[LogicalEvent, ...]


class EventPayload(Contract):
    data: dict[str, Any]
