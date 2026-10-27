"""S11 只读合并候选的有界证据与冲突；不包含批准或执行权限。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, Digest


class ConflictKind(StrEnum):
    WRITE_WRITE = "WRITE_WRITE"
    FACT_CONTRADICTION = "FACT_CONTRADICTION"
    TEMPORAL_MISMATCH = "TEMPORAL_MISMATCH"
    PLAN_DEPENDENCY = "PLAN_DEPENDENCY"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"
    EFFECT_CONFLICT = "EFFECT_CONFLICT"
    PROVENANCE_MISSING = "PROVENANCE_MISSING"


class MergeConflict(Contract):
    kind: ConflictKind
    paths: Annotated[tuple[str, ...], Field(min_length=1, max_length=10)]
    branches: tuple[Literal["base", "ours", "theirs"], ...] = ("base", "ours", "theirs")
    evidence_refs: Annotated[tuple[Digest, ...], Field(max_length=3)] = ()
    severity: Literal["blocking"] = "blocking"
    proposed_resolutions: tuple[str, ...] = ("人工编辑候选后重新验证",)


class MergeBases(Contract):
    ours: Digest
    theirs: Digest
    bases: Annotated[tuple[Digest, ...], Field(max_length=10000)]
    selected: Digest | None = None
    status: Literal["selected", "ambiguous_merge_base", "unrelated"]

    @model_validator(mode="after")
    def coherent_selection(self) -> "MergeBases":
        if self.bases != tuple(sorted(set(self.bases))):
            raise ValueError("最佳共同祖先必须去重并稳定排序")
        if self.selected is not None and self.selected not in self.bases:
            raise ValueError("选定基点不在最佳共同祖先内")
        expected = (
            "selected"
            if self.selected is not None
            else ("ambiguous_merge_base" if len(self.bases) > 1 else "unrelated")
        )
        if self.status != expected or (len(self.bases) == 1 and self.selected is None):
            raise ValueError("共同祖先状态与选定结果不一致")
        return self
