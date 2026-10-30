"""四角色固定执行、显式导出和调用前检查点契约。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.collaboration import CollaborationRole, SharedArtifactHead
from agentgit.core.models import Contract, Digest, EffectClass, Identifier, RunStatus
from agentgit.core.research import BudgetUsage, CallTrace, Evidence, ResearchGoal

RoleAction = Literal["delegate", "collect", "verify", "write", "complete"]


class RoleDecision(Contract):
    role: CollaborationRole
    action: RoleAction


class DispatchExport(Contract):
    kind: Literal["dispatch"] = "dispatch"
    program_ref: Digest
    task: Literal["collect", "verify", "write"]


class EvidenceExport(Contract):
    kind: Literal["evidence"] = "evidence"
    evidence: Annotated[tuple[Evidence, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def distinct_sources(self) -> "EvidenceExport":
        if len({item.source.id for item in self.evidence}) != len(self.evidence):
            raise ValueError("协作证据不能重复来源")
        return self


class VerificationExport(Contract):
    kind: Literal["verification"] = "verification"
    evidence_ref: Digest
    source_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    evaluation_time: str
    accepted: Literal[True] = True


class ResultExport(Contract):
    kind: Literal["result"] = "result"
    evidence_ref: Digest
    verification_ref: Digest
    report_ref: Digest
    sources_ref: Digest


class RoleModelRequest(Contract):
    role: CollaborationRole
    action: RoleAction
    goal: ResearchGoal
    private_notes: tuple[str, ...]
    inbox_payloads: Annotated[tuple[Digest, ...], Field(max_length=8)] = ()
    inbox: Annotated[
        tuple[DispatchExport | EvidenceExport | VerificationExport | ResultExport, ...],
        Field(max_length=8),
    ] = ()


class PendingRoleCall(Contract):
    category: Literal["model", "tool"]
    name: str
    step: Annotated[str, Field(min_length=1, max_length=100)]
    provider: str
    signature: Digest
    effect: EffectClass
    request_ref: Digest
    max_bytes: Annotated[int, Field(ge=1, le=1_048_576)]


class CollaborationRoleState(Contract):
    kind: Literal["collaboration_role/v1"] = "collaboration_role/v1"
    collaboration_id: Identifier
    role: CollaborationRole
    program_ref: Digest
    private_input_ref: Digest
    status: RunStatus = RunStatus.RUNNING
    phase: Literal["initial", "work", "export_ready", "await_result", "done"] = "initial"
    usage: BudgetUsage = BudgetUsage()
    pending: PendingRoleCall | None = None
    decisions: Annotated[tuple[RoleDecision, ...], Field(max_length=2)] = ()
    traces: Annotated[tuple[CallTrace, ...], Field(max_length=32)] = ()
    received: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()
    sent: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()
    source_ids: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    evidence: Annotated[tuple[Evidence, ...], Field(max_length=32)] = ()
    exports: dict[str, Digest] = Field(default_factory=dict)
    publications: Annotated[tuple[SharedArtifactHead, ...], Field(max_length=4)] = ()
    error_code: str | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> "CollaborationRoleState":
        if len(set(self.received)) != len(self.received) or len(set(self.sent)) != len(self.sent):
            raise ValueError("角色检查点不能重复消息")
        if any(decision.role != self.role for decision in self.decisions):
            raise ValueError("模型决策必须属于当前角色")
        if self.usage.steps != self.usage.model_calls + self.usage.tool_calls:
            raise ValueError("固定角色步骤按模型/工具动作分别计量")
        if self.pending is None and self.usage.reserved_tokens:
            raise ValueError("没有模型调用时不能残留预留")
        if self.pending is not None and self.phase == "done":
            raise ValueError("终态不能保留未观察调用")
        if set(self.exports) - {
            "evidence.json",
            "verification.json",
            "report.md",
            "sources.json",
            "message",
            "message:collect",
            "message:verify",
            "message:write",
        }:
            raise ValueError("角色导出仅允许固定产物和消息")
        return self
