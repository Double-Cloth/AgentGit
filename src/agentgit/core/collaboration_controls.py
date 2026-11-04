"""操作者组控制与收据；接受请求不代表在途调用已经结束。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, Digest, Identifier, NonNegative

GroupAction = Literal["pause", "resume", "cancel"]


class CollaborationControlRequest(Contract):
    action: GroupAction
    expected_revision: NonNegative


class CollaborationControlFact(Contract):
    kind: Literal["collaboration-control/v1"] = "collaboration-control/v1"
    collaboration_id: Identifier
    revision: Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
    action: GroupAction
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    managed_runs: Annotated[tuple[Identifier, ...], Field(max_length=4)]

    @model_validator(mode="after")
    def unique_runs(self) -> "CollaborationControlFact":
        if len(set(self.managed_runs)) != len(self.managed_runs):
            raise ValueError("组控制不能重复登记角色 Run")
        return self


class CollaborationControlReceipt(Contract):
    kind: Literal["collaboration-control-receipt/v1"] = "collaboration-control-receipt/v1"
    collaboration_id: Identifier
    action: GroupAction
    revision: NonNegative
    decision: Literal["accepted"] = "accepted"
    source_run_id: Identifier
    source_seq: Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
    source_payload_ref: Digest
