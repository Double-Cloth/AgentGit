"""严格二级 Markdown 章节与既有研究报告 Schema 的只读三方合并。"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import ConflictKind, MergeConflict
from agentgit.core.models import parse_contract
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.snapshots import ArtifactEntry
from agentgit.diff.structural import pointer_part
from agentgit.runtime.validation import ResearchReport

MAX_FILE_BYTES = 1024 * 1024
MAX_ENTITIES = 1000
_MISSING = object()
_CONFLICT = object()


@dataclass(frozen=True)
class ArtifactVersion:
    entry: ArtifactEntry
    content: bytes


@dataclass(frozen=True)
class ArtifactCandidate:
    path: str
    media_type: str
    status: Literal["merged", "deleted", "conflict"]
    content: bytes | None
    evidence_refs: tuple[str, ...]
    conflicts: tuple[MergeConflict, ...]
    validation_pending: tuple[str, ...] = ("state", "provenance", "task_constraints", "approval")


def _same(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return encode(left) == encode(right)


def _select(base: Any, ours: Any, theirs: Any) -> Any:
    if _same(ours, theirs):
        return ours
    if _same(ours, base):
        return theirs
    if _same(theirs, base):
        return ours
    return _CONFLICT


def _markdown(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeError:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Markdown 必须是有效 UTF-8") from None
    if text.startswith("\ufeff") or "\x00" in text:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Markdown 不支持 BOM 或 NUL")
    sections: dict[str, list[str]] = {"": []}
    key = ""
    fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$", line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        elif line.startswith("## "):
            key = line[3:].strip()
            if not key or key in sections:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Markdown 章节标题为空或重复")
            if len(sections) > MAX_ENTITIES:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Markdown 章节超过上限")
            sections[key] = []
        sections[key].append(line)
    if fence is not None:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Markdown 代码围栏未闭合")
    return {key: "".join(lines) for key, lines in sections.items()}


def _report(content: bytes) -> dict[str, Any]:
    report = parse_contract(content, ResearchReport).model_dump(mode="json")
    if report["fixture"] is not True:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "首版只支持声明的研究 fixture")
    for name in ("claims", "sources"):
        if len(report[name]) > MAX_ENTITIES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "研究报告实体超过上限")
        identities = [
            (item["framework"], item["dimension"]) if name == "claims" else item["source"]["id"]
            for item in report[name]
        ]
        if len(set(identities)) != len(identities):
            raise AgentGitError(ErrorCode.VALIDATION, "研究报告实体身份重复")
    return report


def merge_artifact(
    path: str,
    base: ArtifactVersion | None,
    ours: ArtifactVersion | None,
    theirs: ArtifactVersion | None,
) -> ArtifactCandidate:
    validate_artifact_paths([path])
    versions = (base, ours, theirs)
    present = [value for value in versions if value is not None]
    if not present:
        raise AgentGitError(ErrorCode.VALIDATION, "三方均不存在该产物")
    media = "application/json" if path == "sources.json" else "text/markdown"
    if path != "sources.json" and not path.endswith(".md"):
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "只支持 Markdown 与 sources.json")
    refs = tuple(dict.fromkeys(value.entry.object_ref for value in present))
    prefix = "/artifacts/" + pointer_part(path)
    conflicts: list[MergeConflict] = []

    def conflict(location: str, kind: ConflictKind = ConflictKind.WRITE_WRITE) -> None:
        conflicts.append(MergeConflict(kind=kind, paths=(location,), evidence_refs=refs))

    documents: list[Any] = []
    for value in versions:
        if value is None:
            documents.append(_MISSING)
            continue
        if value.entry.path != path or value.entry.media_type != media:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "产物路径或 MIME 与支持协议不符")
        if len(value.content) > MAX_FILE_BYTES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并输入超过文件上限")
        if object_digest(ObjectType.BLOB, value.content) != value.entry.object_ref:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并产物字节与 BLOB 摘要不符")
        documents.append(
            _report(value.content) if path == "sources.json" else _markdown(value.content)
        )

    def table_merge(
        tables: list[dict[str, Any]], location: str, kind: ConflictKind
    ) -> dict[str, Any]:
        result = {}
        for key in sorted(set().union(*(set(table) for table in tables))):
            selected = _select(*(table.get(key, _MISSING) for table in tables))
            if selected is _CONFLICT:
                conflict(location + "/" + pointer_part(key), kind)
            elif selected is not _MISSING:
                result[key] = selected
        return result

    # 整文件缺失与编辑相遇时不能改成空文档；保留删除/编辑冲突。
    if any(document is _MISSING for document in documents):
        raw = [value.content.decode("utf-8") if value else _MISSING for value in versions]
        selected = _select(*raw)
        if selected is _CONFLICT:
            conflict(prefix)
        elif selected is _MISSING:
            return ArtifactCandidate(path, media, "deleted", None, refs, ())
        else:
            index = next(index for index, document in enumerate(raw) if document is selected)
            original = versions[index]
            assert original is not None
            return ArtifactCandidate(path, media, "merged", original.content, refs, ())
    elif path.endswith(".md"):
        original_order = list(documents[0])
        for document in documents[1:]:
            shared = [key for key in document if key in documents[0]]
            if shared != [key for key in original_order if key in document]:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不支持既有 Markdown 章节重排")
        merged = table_merge(documents, prefix, ConflictKind.WRITE_WRITE)
        order = [key for key in original_order if key in merged]
        order += sorted(set(merged) - set(original_order))
        content = "".join(merged[key] for key in order).encode("utf-8")
        if not conflicts and _markdown(content) != merged:
            conflict(prefix + "/@layout")
    else:
        merged = {"schema_version": 1}
        for name in ("fixture", "evaluation_time", "frameworks"):
            values = [document[name] for document in documents]
            if not all(_same(values[0], item) for item in values[1:]) or (
                name == "fixture" and values[0] is not True
            ):
                conflict(
                    prefix + "/" + name,
                    ConflictKind.TEMPORAL_MISMATCH
                    if name == "evaluation_time"
                    else ConflictKind.CONSTRAINT_VIOLATION,
                )
            merged[name] = values[0]
        for name in ("claims", "sources"):
            tables = []
            for document in documents:
                table = {}
                for item in document[name]:
                    key = (
                        encode([item["framework"], item["dimension"]]).decode("utf-8")
                        if name == "claims"
                        else item["source"]["id"]
                    )
                    if key in table:
                        raise AgentGitError(ErrorCode.VALIDATION, "研究报告实体身份重复")
                    table[key] = item
                tables.append(table)
            entities = table_merge(
                tables,
                prefix + "/" + name,
                ConflictKind.FACT_CONTRADICTION if name == "claims" else ConflictKind.WRITE_WRITE,
            )
            order = [key for key in tables[0] if key in entities]
            order += sorted(set(entities) - set(tables[0]))
            merged[name] = [entities[key] for key in order]
        content = encode(parse_contract(encode(merged), ResearchReport).model_dump(mode="json"))
    if conflicts:
        return ArtifactCandidate(path, media, "conflict", None, refs, tuple(conflicts))
    if len(content) > MAX_FILE_BYTES:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并候选超过文件上限")
    return ArtifactCandidate(path, media, "merged", content, refs, ())
