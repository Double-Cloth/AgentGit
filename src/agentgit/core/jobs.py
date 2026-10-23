"""持久化调度的操作身份与冻结请求。"""

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from agentgit.core.models import Contract, Digest, Identifier, NonNegative
from agentgit.core.research import BudgetLimits, Source
from agentgit.core.versions import HybridReplaySpec, LiveReplaySpec, RecordedReplaySpec

TERMINAL_RUNS = frozenset({"completed", "failed", "cancelled", "budget_exhausted"})


class JobPayload(Contract):
    replay: (
        Annotated[
            RecordedReplaySpec | LiveReplaySpec | HybridReplaySpec, Field(discriminator="mode")
        ]
        | None
    ) = None
    limits: BudgetLimits | None = None
    kind: Literal["research_run"] = "research_run"
    config_id: Identifier
    goal_ref: Digest
    code_revision: str
    corpus_digest: Digest
    sources: tuple[Source, ...]
    evaluation_time: str
    initial_plan_version: NonNegative = 0
    max_wall_ms: Annotated[int, Field(ge=1)]


class Submission(Contract):
    job_id: Identifier
    run_id: Identifier


@dataclass(frozen=True)
class Lease:
    job_id: UUID
    repository_id: UUID
    run_id: UUID
    worker: str
    fencing_token: int
    lease_ms: int
    recovery_required: bool = False
