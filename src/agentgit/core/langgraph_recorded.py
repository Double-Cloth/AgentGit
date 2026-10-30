"""固定图 Recorded 的不可变来源范围和控制依据。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.versions import NewBranch


class NewLangGraphRecorded(NewBranch):
    to_commit: Digest


class LangGraphRecordedSpec(Contract):
    source_run_id: Identifier
    from_commit: Digest
    to_commit: Digest
    bundle_ref: Digest


class LangGraphRecordedControl(Contract):
    kind: Literal["requested", "native", "initial"]
    before_digest: Digest | None
    paused_digest: Digest
    resumed_digest: Digest
    pause_event_ref: Digest
    pause_request_ref: Digest | None
    resume_event_ref: Digest
    resume_request_ref: Digest

    @model_validator(mode="after")
    def complete_basis(self) -> "LangGraphRecordedControl":
        if (self.kind == "initial") != (self.before_digest is None) or (
            self.kind == "requested" and self.pause_request_ref is None
        ):
            raise ValueError("图录制控制必须有明确起点和请求依据")
        return self


class LangGraphRecordingBundle(Contract):
    kind: Literal["langgraph_recording/v1"] = "langgraph_recording/v1"
    source_run_id: Identifier
    from_commit: Digest
    to_commit: Digest
    start_commit: Digest
    program_ref: Digest
    source_payload_ref: Digest
    from_state_ref: Digest
    start_state_ref: Digest
    to_state_ref: Digest
    environment_ref: Digest
    call_id: Identifier
    intent_ref: Digest
    result_ref: Digest
    needs_fetch: bool
    needs_fork_initialization: bool
    controls: Annotated[tuple[LangGraphRecordedControl, ...], Field(max_length=64)]
