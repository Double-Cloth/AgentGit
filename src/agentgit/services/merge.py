"""S11 同项目真实快照的只读基点与单文件候选，不发布提案或 Commit。"""

from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import MergeBases
from agentgit.core.models import CommitManifest, parse_contract
from agentgit.merge.ancestry import merge_bases
from agentgit.merge.artifacts import ArtifactCandidate, ArtifactVersion, merge_artifact
from agentgit.services.history import TABLES, HistoryService


class MergePreviewService:
    def __init__(self, history: HistoryService):
        self.history = history

    def bases(
        self, repository_id: UUID, ours: str, theirs: str, *, selected: str | None = None
    ) -> MergeBases:
        with self.history.engine.connect() as connection:

            def parents(commit_id: str) -> tuple[str, ...]:
                row = self.history._commit_row(connection, repository_id, commit_id)
                table = TABLES["commit_parents"]
                values = tuple(
                    connection.execute(
                        sa.select(table.c.parent_id)
                        .where(
                            table.c.repository_id == repository_id,
                            table.c.commit_id == commit_id,
                        )
                        .order_by(table.c.parent_order)
                    ).scalars()
                )
                manifest = parse_contract(
                    self.history.objects.get(commit_id, ObjectType.COMMIT), CommitManifest
                )
                if (
                    manifest.repository_id != str(repository_id)
                    or manifest.parents != values
                    or any(
                        getattr(manifest, key) != row[key]
                        for key in ("state_ref", "environment_ref", "event_manifest_ref")
                    )
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并基点历史索引与内容不符")
                return values

            return merge_bases(ours, theirs, parents, selected=selected)

    def artifact(
        self,
        repository_id: UUID,
        ours: str,
        theirs: str,
        path: str,
        *,
        selected_base: str | None = None,
    ) -> tuple[MergeBases, ArtifactCandidate]:
        selection = self.bases(repository_id, ours, theirs, selected=selected_base)
        if selection.selected is None:
            # 先通过 bases 查询完整歧义集合；本预览没有自动选择或写入权。
            raise AgentGitError(ErrorCode.VALIDATION, selection.status)
        versions = []
        for commit_id in (selection.selected, ours, theirs):
            snapshot = self.history.restore(repository_id, commit_id)
            if len(snapshot.artifacts) > 1000:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "产物数量超过合并预览上限")
            entry = next((item for item in snapshot.artifacts if item.path == path), None)
            versions.append(
                ArtifactVersion(entry, self.history.objects.get(entry.object_ref, ObjectType.BLOB))
                if entry is not None
                else None
            )
        return selection, merge_artifact(path, *versions)
