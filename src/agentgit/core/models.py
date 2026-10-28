"""不可变、拒绝未知字段的首版内容契约。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agentgit.core.canonical import decode
from agentgit.core.errors import AgentGitError, ErrorCode

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
NonNegative = Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema 版本必须为整数")
        return value


def parse_contract[T: Contract](payload: bytes, model: type[T]) -> T:
    value = decode(payload)
    if not isinstance(value, dict):
        raise AgentGitError(ErrorCode.VALIDATION, "内容契约必须为 object")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不支持或缺失的 Schema 版本")
    try:
        return model.model_validate_json(payload)
    except ValidationError:
        raise AgentGitError(ErrorCode.VALIDATION, "内容不符合声明的 Schema") from None


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ReplayMode(StrEnum):
    LIVE = "live"
    RECORDED = "recorded"
    HYBRID = "hybrid"


class EffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"
    IRREVERSIBLE = "IRREVERSIBLE"


class CallOccurrence(Contract):
    logical_step_id: Annotated[str, Field(min_length=1, max_length=200)]
    call_index: NonNegative
    attempt: Annotated[int, Field(ge=1)]
    provider: Annotated[str, Field(min_length=1, max_length=200)]
    signature_digest: Digest
    request_digest: Digest


class ModelCallBudget(Contract):
    tokens: NonNegative
    currency: Literal["USD"] = "USD"
    cost_micro_usd: NonNegative
    pricebook: Annotated[str, Field(min_length=1, max_length=100)]


class CallIntent(Contract):
    occurrence: CallOccurrence
    effect: EffectClass
    request_ref: Digest
    checkpoint: Digest | None
    max_response_bytes: Annotated[int, Field(ge=1, le=8 * 1024 * 1024)]
    model_budget: ModelCallBudget | None = Field(default=None, exclude_if=lambda v: v is None)


class EventRange(Contract):
    first_seq: Annotated[int, Field(ge=1)]
    last_seq: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def ordered(self) -> "EventRange":
        if self.last_seq < self.first_seq:
            raise ValueError("事件范围不能倒置")
        return self


class CommitManifest(Contract):
    codec: Literal["AGJSON-v1"] = "AGJSON-v1"
    repository_id: Identifier
    parents: Annotated[tuple[Digest, ...], Field(max_length=2)] = ()
    state_ref: Digest
    environment_ref: Digest
    event_manifest_ref: Digest
    event_range: EventRange

    @model_validator(mode="after")
    def distinct_parents(self) -> "CommitManifest":
        if len(set(self.parents)) != len(self.parents):
            raise ValueError("Commit 父关系必须不同")
        return self


class EnvironmentManifest(Contract):
    codec: Literal["AGJSON-v1"] = "AGJSON-v1"
    runtime_version: str
    python_version: str
    code_revision: str
    dependency_lock_digest: Digest
    model_signature: str
    prompt_digest: Digest
    tool_signatures: tuple[Digest, ...] = ()
    input_ref: Digest
    evaluation_time: str
    random_seed: NonNegative


class ObjectEnvelope(Contract):
    object_id: Digest
    codec: Literal["AGJSON-v1"] = "AGJSON-v1"
    object_type: str
    media_type: str
    byte_length: NonNegative
    storage_key: str
