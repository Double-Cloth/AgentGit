"""耐久合并请求、完整语义编辑、候选修订和确切审阅；不授予执行权。"""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from agentgit.core.merge import MergeConflict, MergedResearchState, MergeOrigin, MergeValidation
from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.research import Plan
from agentgit.core.versions import ArtifactPatch, NewBranch


class MergeBranchPin(Contract):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    source_run_id: Identifier
    head: Digest
    version: Annotated[int, Field(ge=0)]

    @field_validator("name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        return NewBranch.visible_name(value)


class NewMergeProposal(Contract):
    ours: MergeBranchPin
    theirs: MergeBranchPin
    validator_ref: Digest
    selected_base: Digest | None = None

    @model_validator(mode="after")
    def distinct_sides(self) -> "NewMergeProposal":
        if self.ours.name == self.theirs.name or self.ours.head == self.theirs.head:
            raise ValueError("提案需要两个不同分支及不同 HEAD")
        return self


class MergeEdits(Contract):
    replace_plan: bool = False
    plan: Plan | None = None
    sources: Annotated[
        dict[str, Literal["base", "ours", "theirs", "delete"]], Field(max_length=1000)
    ] = Field(default_factory=dict)
    artifacts: Annotated[tuple[ArtifactPatch, ...], Field(max_length=2)] = ()

    @model_validator(mode="after")
    def bounded_edits(self) -> "MergeEdits":
        if self.plan is not None and not self.replace_plan:
            raise ValueError("计划编辑必须明确选择替换")
        paths = [item.path for item in self.artifacts]
        if len(set(paths)) != len(paths) or set(paths) - {"report.md", "sources.json"}:
            raise ValueError("编辑只支持两个不同的研究产物")
        if any(
            item.operation == "write"
            and (
                item.text is None
                or item.media_type
                not in (None, "text/markdown" if item.path == "report.md" else "application/json")
            )
            for item in self.artifacts
        ):
            raise ValueError("合并编辑只支持对应 MIME 的 UTF-8 文本")
        if any(len(item.content()) > 1024 * 1024 for item in self.artifacts):
            raise ValueError("单个编辑文件超过 1 MiB")
        return self


class ReplaceMergeRevision(Contract):
    expected_revision: Annotated[int, Field(ge=1, le=100)]
    expected_revision_ref: Digest
    edits: MergeEdits


class ValidateMergeRevision(Contract):
    expected_revision: Annotated[int, Field(ge=1, le=100)]
    expected_revision_ref: Digest


class PrepareMergeReview(ValidateMergeRevision):
    validation_id: Identifier


class RejectMergeProposal(ValidateMergeRevision):
    reason: Annotated[str, Field(min_length=1, max_length=4000)]


class MergeProposalManifest(Contract):
    kind: Literal["merge_proposal/v1"] = "merge_proposal/v1"
    request: NewMergeProposal
    origins: Annotated[tuple[MergeOrigin, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def exact_sources(self) -> "MergeProposalManifest":
        for pin, origin in zip(
            (self.request.ours, self.request.theirs), self.origins[1:], strict=True
        ):
            if pin.source_run_id != origin.run_id or pin.head != origin.commit_id:
                raise ValueError("合并提案必须绑定确切两侧来源")
        if (
            self.request.selected_base is not None
            and self.request.selected_base != self.origins[0].commit_id
        ):
            raise ValueError("提案基点与显式选择不同")
        return self


class MergeArtifactDocument(Contract):
    path: Literal["report.md", "sources.json"]
    media_type: Literal["text/markdown", "application/json"]
    status: Literal["merged", "deleted", "conflict"]
    text: Annotated[str | None, Field(max_length=1024 * 1024)]
    evidence_refs: Annotated[tuple[Digest, ...], Field(max_length=3)]

    @model_validator(mode="after")
    def exact_artifact(self) -> "MergeArtifactDocument":
        if (self.status == "merged") != (self.text is not None):
            raise ValueError("只有无冲突且未删除的产物可带候选正文")
        if self.media_type != ("text/markdown" if self.path == "report.md" else "application/json"):
            raise ValueError("合并产物 MIME 与路径不匹配")
        return self


class MergeRevisionBody(Contract):
    kind: Literal["merge_revision/v1"] = "merge_revision/v1"
    proposal_ref: Digest
    revision: Annotated[int, Field(ge=1, le=100)]
    previous_ref: Digest | None
    edits: MergeEdits
    state: MergedResearchState | None
    artifacts: Annotated[tuple[MergeArtifactDocument, ...], Field(max_length=2)]
    conflicts: Annotated[tuple[MergeConflict, ...], Field(max_length=1000)]
    candidate_digest: Digest
    code_revision: str

    @model_validator(mode="after")
    def linear_revision(self) -> "MergeRevisionBody":
        if (self.revision == 1) != (self.previous_ref is None):
            raise ValueError("修订必须绑定确切前版")
        if len({item.path for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("候选产物不能重复")
        return self


class MergeValidationRecord(Contract):
    kind: Literal["merge_validation_record/v1"] = "merge_validation_record/v1"
    proposal_ref: Digest
    revision_ref: Digest
    result: MergeValidation


class MergeReviewTicket(Contract):
    kind: Literal["merge_review_ticket/v1"] = "merge_review_ticket/v1"
    proposal_ref: Digest
    revision_ref: Digest
    validation_id: Identifier
    validation_ref: Digest
    candidate_digest: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    ours: MergeBranchPin
    theirs: MergeBranchPin
    execution: Literal["not_started"] = "not_started"


class MergeRejection(Contract):
    kind: Literal["merge_rejection/v1"] = "merge_rejection/v1"
    proposal_ref: Digest
    revision_ref: Digest
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=4000)]
    decision: Literal["reject"] = "reject"
    execution: Literal["not_started"] = "not_started"
