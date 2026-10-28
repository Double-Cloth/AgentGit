"""读取三方真实状态/产物并进行冻结硬验证；输出仍须审批的只读候选。"""

from dataclasses import dataclass, replace
from uuid import UUID

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import (
    ConflictKind,
    MergeBases,
    MergeConflict,
    MergeOrigin,
    MergeValidation,
)
from agentgit.core.merge_proposals import MergeEdits
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.research import AgentState
from agentgit.experiments.validation import frozen_inputs
from agentgit.merge.artifacts import ArtifactCandidate, ArtifactVersion, merge_artifact
from agentgit.merge.state import StateCandidate, merge_state
from agentgit.runtime.validation import validate_report
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import HistoryService
from agentgit.services.merge import MergePreviewService
from agentgit.services.merge_inputs import base_origin, load_state_input
from agentgit.services.validators import ValidatorService


@dataclass(frozen=True)
class ResearchMergePreview:
    selection: MergeBases
    state: StateCandidate
    artifacts: tuple[ArtifactCandidate, ...]
    validation: MergeValidation


def preview_research_merge(
    history: HistoryService,
    repository: UUID,
    ours: MergeOrigin,
    theirs: MergeOrigin,
    validator_ref: str,
    *,
    selected_base: str | None = None,
    edits: MergeEdits | None = None,
) -> ResearchMergePreview:
    edits = edits or MergeEdits()
    revision, lock = source_revision(), dependency_lock_digest()
    manifest = ValidatorService(history).manifest(repository, validator_ref)
    goal, corpus = frozen_inputs(history.objects, manifest)
    selection = MergePreviewService(history).bases(
        repository, ours.commit_id, theirs.commit_id, selected=selected_base
    )
    if selection.selected is None:
        raise AgentGitError(ErrorCode.VALIDATION, selection.status)
    base = base_origin(history, repository, UUID(ours.run_id), selection.selected)
    origins = (base, ours, theirs)
    inputs = tuple(load_state_input(history, repository, origin, corpus) for origin in origins)
    if any(item.state.goal != goal for item in inputs):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "合并任务与冻结验收器不一致")
    state = merge_state(inputs[0], inputs[1], inputs[2], corpus, edits=edits)
    conflicts = list(state.conflicts)
    snapshots = tuple(history.restore(repository, origin.commit_id) for origin in origins)
    tables = [{item.path: item for item in snapshot.artifacts} for snapshot in snapshots]
    patches = {patch.path: patch for patch in edits.artifacts}
    paths = sorted(set().union(*(set(table) for table in tables), set(patches)))
    if len(paths) > 1000:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并产物数量超过上限")
    if set(paths) - {"report.md", "sources.json"}:
        raise AgentGitError(
            ErrorCode.UNSUPPORTED_SCHEMA, "完整研究验证只支持报告及其结构化来源文件"
        )
    validate_artifact_paths(paths)
    candidates = []
    total = 0
    for path in paths:
        versions = []
        for table in tables:
            entry = table.get(path)
            content = history.objects.get(entry.object_ref, ObjectType.BLOB) if entry else None
            total += len(content) if content is not None else 0
            if total > 16 * 1024 * 1024:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "三方产物字节超过预览上限")
            versions.append(
                ArtifactVersion(entry, content) if entry and content is not None else None
            )
        if path in patches:
            patch = patches[path]
            candidate = ArtifactCandidate(
                path=path,
                media_type="application/json" if path == "sources.json" else "text/markdown",
                status="deleted" if patch.operation == "delete" else "merged",
                content=None if patch.operation == "delete" else patch.content(),
                evidence_refs=tuple(
                    dict.fromkeys(value.entry.object_ref for value in versions if value is not None)
                ),
                conflicts=(),
            )
        else:
            candidate = merge_artifact(path, *versions)
        candidates.append(candidate)
        conflicts.extend(candidate.conflicts)
    projected = state.proposed_state
    contents = {item.path: item.content for item in candidates if item.content is not None}
    refs = tuple(dict.fromkeys(item.state_ref for item in inputs))
    if projected is not None:
        limits = projected.constraints.limits
        if len(contents) > limits.max_files or any(
            len(content) > limits.max_artifact_bytes for content in contents.values()
        ):
            conflicts.append(
                MergeConflict(
                    kind=ConflictKind.CONSTRAINT_VIOLATION,
                    paths=("/artifacts",),
                    evidence_refs=refs,
                )
            )
    evaluation = None
    if projected is not None and not conflicts:
        # 仅借用现有硬规则的输入结构，不伪造已完成 Run、状态或 FrozenAssessment。
        inspection = AgentState(
            goal=projected.goal, plan=projected.plan, working_memory=projected.working_memory
        )
        evaluation = validate_report(inspection, contents, corpus)
        for error in evaluation.errors:
            kind = ConflictKind.PROVENANCE_MISSING
            if error.startswith(("source_expired", "source_future")):
                kind = ConflictKind.TEMPORAL_MISMATCH
            elif error.startswith(("claim_", "duplicate_claim")):
                kind = ConflictKind.FACT_CONTRADICTION
            elif error in (
                "fixture_or_evaluation_time",
                "framework_coverage",
                "report_structure_or_claim_mismatch",
                "report_schema",
            ):
                kind = ConflictKind.CONSTRAINT_VIOLATION
            conflicts.append(
                MergeConflict(kind=kind, paths=("/validation/" + error,), evidence_refs=refs)
            )
    if len(conflicts) > 1000:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并冲突超过上限")
    digest = object_digest(
        ObjectType.BLOB,
        encode(
            dict(
                schema_version=1,
                origins=[origin.model_dump(mode="json") for origin in origins],
                validator_ref=validator_ref,
                source_revision=revision,
                dependency_lock_digest=lock,
                state=projected.model_dump(mode="json") if projected else None,
                artifacts=[
                    dict(
                        path=item.path,
                        media_type=item.media_type,
                        status=item.status,
                        content_ref=object_digest(ObjectType.BLOB, item.content)
                        if item.content is not None
                        else None,
                    )
                    for item in candidates
                ],
            )
        ),
    )
    validation = MergeValidation(
        origins=origins,
        validator_ref=validator_ref,
        candidate_digest=digest,
        code_revision=revision,
        dependency_lock_digest=lock,
        conflicts=tuple(conflicts),
        report_evaluation=evaluation,
        validated=not conflicts and evaluation is not None and evaluation.passed,
    )
    if revision != source_revision() or lock != dependency_lock_digest():
        raise AgentGitError(ErrorCode.VERSION_CONFLICT, "验证期间源码或锁发生变化，请重新验证")
    if validation.validated:
        candidates = [replace(item, validation_pending=("approval",)) for item in candidates]
    return ResearchMergePreview(selection, state, tuple(candidates), validation)
