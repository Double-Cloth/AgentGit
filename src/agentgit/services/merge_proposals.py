"""耐久合并提案与精确修订；验证/审阅准备/拒绝均不写执行历史。"""

from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import MergeConflict, MergeOrigin
from agentgit.core.merge_proposals import (
    MergeArtifactDocument,
    MergeEdits,
    MergeProposalManifest,
    MergeRejection,
    MergeReviewTicket,
    MergeRevisionBody,
    MergeValidationRecord,
    NewMergeProposal,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)
from agentgit.core.models import Contract, parse_contract
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.merge_validation import ResearchMergePreview, preview_research_merge


class MergeProposalService:
    def __init__(self, history: HistoryService):
        self.history = history

    @staticmethod
    def _identity(repo: UUID, payload: Contract, actor: str, key: str) -> str:
        if any(
            not value
            or value != value.strip()
            or len(value) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in (actor, key)
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "合并操作者和幂等键必须是有界可见字符串")
        return object_digest(
            ObjectType.BLOB,
            encode(dict(repository_id=str(repo), request=payload.model_dump(mode="json"))),
        )

    def _lock(self, connection: Connection, repo: UUID, actor: str, route: str, key: str) -> None:
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(encode([actor, route, key]).decode(), 0)
                )
            )
        )
        table = TABLES["repositories"]
        if (
            connection.execute(
                sa.select(table.c.id).where(table.c.id == repo).with_for_update()
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")

    @staticmethod
    def _cached(
        connection: Connection, name: str, actor: str, route: str, key: str, digest: str
    ) -> RowMapping | None:
        table = TABLES[name]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.actor == actor, table.c.route == route, table.c.request_key == key
                )
            )
            .mappings()
            .first()
        )
        if row is not None and row["request_digest"] != digest:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一合并幂等键不能用于不同请求")
        return row

    def _store(
        self,
        connection: Connection,
        repo: UUID,
        identity: UUID,
        owner: str,
        role: str,
        value: Contract,
    ) -> str:
        value = parse_contract(encode(value.model_dump(mode="json")), type(value))
        obj = self.history.objects.put_json(ObjectType.BLOB, value.model_dump(mode="json"))
        self.history._ref(connection, repo, obj, owner, str(identity), role)
        return obj.id

    def _owned[T: Contract](
        self,
        connection: Connection,
        repo: UUID,
        row: RowMapping,
        field: str,
        owner: str,
        role: str,
        model: type[T],
    ) -> T:
        refs = TABLES["object_refs"]
        if (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repo,
                    refs.c.object_id == row[field],
                    refs.c.owner_type == owner,
                    refs.c.owner_id == str(row["id"]),
                    refs.c.role == role,
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并记录缺少同项目不可变内容引用")
        return parse_contract(self.history.objects.get(row[field], ObjectType.BLOB), model)

    def _root(
        self, connection: Connection, repo: UUID, identity: UUID
    ) -> tuple[RowMapping, MergeProposalManifest]:
        table = TABLES["merge_proposals"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.repository_id == repo, table.c.id == identity)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "合并提案不可访问")
        value = self._owned(
            connection,
            repo,
            row,
            "manifest_ref",
            "merge_proposal",
            "manifest",
            MergeProposalManifest,
        )
        for side in ("ours", "theirs"):
            pin = getattr(value.request, side)
            if any(
                row[side + "_" + key] != expected
                for key, expected in (
                    ("branch", pin.name),
                    ("run_id", UUID(pin.source_run_id)),
                    ("head", pin.head),
                    ("version", pin.version),
                )
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并提案分支索引与内容不符")
        if (
            row["base_commit_id"] != value.origins[0].commit_id
            or row["validator_ref"] != value.request.validator_ref
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并提案基点或验收器索引不符")
        return row, value

    def _revision(
        self, connection: Connection, repo: UUID, proposal: UUID, identity: UUID | None = None
    ) -> tuple[RowMapping, MergeRevisionBody]:
        table = TABLES["merge_revisions"]
        query = sa.select(table).where(
            table.c.repository_id == repo, table.c.proposal_id == proposal
        )
        query = (
            query.where(table.c.id == identity)
            if identity is not None
            else query.order_by(table.c.number.desc()).limit(1)
        )
        row = connection.execute(query).mappings().first()
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "合并修订不可访问")
        body = self._owned(
            connection, repo, row, "revision_ref", "merge_revision", "revision", MergeRevisionBody
        )
        root, _ = self._root(connection, repo, proposal)
        if (
            body.proposal_ref != root["manifest_ref"]
            or body.revision != row["number"]
            or body.previous_ref != row["previous_ref"]
            or body.candidate_digest != row["candidate_digest"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并修订索引与内容不符")
        return row, body

    @staticmethod
    def _open(connection: Connection, proposal: UUID) -> None:
        table = TABLES["merge_rejections"]
        if (
            connection.execute(sa.select(table.c.id).where(table.c.proposal_id == proposal)).first()
            is not None
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "合并提案已经拒绝，不能继续修改")

    def _pins(
        self, connection: Connection, repo: UUID, value: MergeProposalManifest | NewMergeProposal
    ) -> None:
        request = value.request if isinstance(value, MergeProposalManifest) else value
        table = TABLES["branches"]
        rows = (
            connection.execute(
                sa.select(table)
                .where(
                    table.c.repository_id == repo,
                    table.c.name.in_((request.ours.name, request.theirs.name)),
                )
                .order_by(table.c.name)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        indexed = {row["name"]: row for row in rows}
        for pin in (request.ours, request.theirs):
            row = indexed.get(pin.name)
            if row is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "合并分支不可访问")
            if (
                row["head_commit_id"] != pin.head
                or row["version"] != pin.version
                or row["active_writer_run_id"] is not None
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "合并分支已变化或仍有活动写入者")
            source = self.history._run(connection, repo, UUID(pin.source_run_id))
            if source["branch_name"] != pin.name:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "合并来源 Run 不属于所选分支")

    def _preview(
        self, repo: UUID, manifest: MergeProposalManifest, edits: MergeEdits
    ) -> ResearchMergePreview:
        result = preview_research_merge(
            self.history,
            repo,
            manifest.origins[1],
            manifest.origins[2],
            manifest.request.validator_ref,
            selected_base=manifest.origins[0].commit_id,
            edits=edits,
        )
        if result.validation.origins != manifest.origins:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "合并来源路径已变化，需要重新建立提案")
        return result

    def _append(
        self,
        connection: Connection,
        repo: UUID,
        proposal: RowMapping,
        revision: int,
        previous: str | None,
        edits: MergeEdits,
        preview: ResearchMergePreview,
        actor: str,
        route: str,
        key: str,
        digest: str,
    ) -> RowMapping:
        identity = uuid4()
        body = MergeRevisionBody(
            proposal_ref=proposal["manifest_ref"],
            revision=revision,
            previous_ref=previous,
            edits=edits,
            state=preview.state.proposed_state,
            artifacts=tuple(
                MergeArtifactDocument.model_validate_json(
                    encode(
                        dict(
                            schema_version=1,
                            path=item.path,
                            media_type=item.media_type,
                            status=item.status,
                            text=item.content.decode("utf-8") if item.content is not None else None,
                            evidence_refs=list(item.evidence_refs),
                        )
                    )
                )
                for item in preview.artifacts
            ),
            conflicts=preview.validation.conflicts,
            candidate_digest=preview.validation.candidate_digest,
            code_revision=preview.validation.code_revision,
        )
        ref = self._store(connection, repo, identity, "merge_revision", "revision", body)
        table = TABLES["merge_revisions"]
        row = (
            connection.execute(
                table.insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    proposal_id=proposal["id"],
                    number=revision,
                    previous_ref=previous,
                    revision_ref=ref,
                    candidate_digest=body.candidate_digest,
                    actor=actor,
                    route=route,
                    request_key=key,
                    request_digest=digest,
                )
                .returning(table)
            )
            .mappings()
            .one()
        )
        self.history.fault("merge.revision_stored")
        return row

    @staticmethod
    def _revision_receipt(row: RowMapping) -> dict[str, Any]:
        return dict(
            proposal_id=str(row["proposal_id"]),
            revision_id=str(row["id"]),
            revision=row["number"],
            revision_ref=row["revision_ref"],
            candidate_digest=row["candidate_digest"],
        )

    @staticmethod
    def _expected(row: RowMapping, request: ValidateMergeRevision | ReplaceMergeRevision) -> None:
        if (
            row["number"] != request.expected_revision
            or row["revision_ref"] != request.expected_revision_ref
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "合并候选已修订，请重新读取当前版本")

    def create(
        self, repo: UUID, request: NewMergeProposal, *, actor: str, key: str
    ) -> dict[str, Any]:
        request = parse_contract(encode(request.model_dump(mode="json")), NewMergeProposal)
        digest = self._identity(repo, request, actor, key)
        route = "POST /merge-proposals"
        with self.history.engine.begin() as connection:
            self._lock(connection, repo, actor, route, key)
            cached = self._cached(connection, "merge_proposals", actor, route, key, digest)
            if cached is not None:
                revisions = TABLES["merge_revisions"]
                row = (
                    connection.execute(
                        sa.select(revisions).where(
                            revisions.c.proposal_id == cached["id"], revisions.c.number == 1
                        )
                    )
                    .mappings()
                    .one()
                )
                return {**self._revision_receipt(row), "proposal_ref": cached["manifest_ref"]}
            self._pins(connection, repo, request)
            preview = preview_research_merge(
                self.history,
                repo,
                MergeOrigin(run_id=request.ours.source_run_id, commit_id=request.ours.head),
                MergeOrigin(run_id=request.theirs.source_run_id, commit_id=request.theirs.head),
                request.validator_ref,
                selected_base=request.selected_base,
            )
            body = MergeProposalManifest(request=request, origins=preview.validation.origins)
            identity = uuid4()
            ref = self._store(connection, repo, identity, "merge_proposal", "manifest", body)
            table = TABLES["merge_proposals"]
            pins = {
                side + "_" + name: value
                for side in ("ours", "theirs")
                for name, value in (
                    ("branch", getattr(request, side).name),
                    ("run_id", UUID(getattr(request, side).source_run_id)),
                    ("head", getattr(request, side).head),
                    ("version", getattr(request, side).version),
                )
            }
            root = (
                connection.execute(
                    table.insert()
                    .values(
                        id=identity,
                        repository_id=repo,
                        manifest_ref=ref,
                        base_commit_id=body.origins[0].commit_id,
                        validator_ref=request.validator_ref,
                        actor=actor,
                        route=route,
                        request_key=key,
                        request_digest=digest,
                        **pins,
                    )
                    .returning(table)
                )
                .mappings()
                .one()
            )
            row = self._append(
                connection, repo, root, 1, None, MergeEdits(), preview, actor, route, key, digest
            )
            return {**self._revision_receipt(row), "proposal_ref": ref}

    def replace(
        self, repo: UUID, proposal: UUID, request: ReplaceMergeRevision, *, actor: str, key: str
    ) -> dict[str, Any]:
        request = parse_contract(encode(request.model_dump(mode="json")), ReplaceMergeRevision)
        digest = self._identity(repo, request, actor, key)
        route = f"POST /merge-proposals/{proposal}/revisions"
        with self.history.engine.begin() as connection:
            self._lock(connection, repo, actor, route, key)
            cached = self._cached(connection, "merge_revisions", actor, route, key, digest)
            if cached is not None:
                return self._revision_receipt(cached)
            root, manifest = self._root(connection, repo, proposal)
            self._open(connection, proposal)
            previous, _ = self._revision(connection, repo, proposal)
            self._expected(previous, request)
            if previous["number"] >= 100:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并提案最多追加 100 个修订")
            self._pins(connection, repo, manifest)
            preview = self._preview(repo, manifest, request.edits)
            row = self._append(
                connection,
                repo,
                root,
                previous["number"] + 1,
                previous["revision_ref"],
                request.edits,
                preview,
                actor,
                route,
                key,
                digest,
            )
            return self._revision_receipt(row)

    def _validation(
        self, connection: Connection, repo: UUID, proposal: UUID, identity: UUID
    ) -> tuple[RowMapping, MergeValidationRecord]:
        table = TABLES["merge_validations"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repo,
                    table.c.proposal_id == proposal,
                    table.c.id == identity,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "合并验证不可访问")
        body = self._owned(
            connection,
            repo,
            row,
            "validation_ref",
            "merge_validation",
            "validation",
            MergeValidationRecord,
        )
        _, revision = self._revision(connection, repo, proposal, row["revision_id"])
        root, manifest = self._root(connection, repo, proposal)
        if (
            body.proposal_ref != root["manifest_ref"]
            or body.result.origins != manifest.origins
            or body.result.validator_ref != manifest.request.validator_ref
            or body.result.candidate_digest != revision.candidate_digest
            or body.result.code_revision != revision.code_revision
            or body.revision_ref
            != object_digest(ObjectType.BLOB, encode(revision.model_dump(mode="json")))
            or body.result.candidate_digest != row["candidate_digest"]
            or body.result.validated != row["passed"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并验证与修订或结果索引不符")
        return row, body

    def validate(
        self, repo: UUID, proposal: UUID, request: ValidateMergeRevision, *, actor: str, key: str
    ) -> dict[str, Any]:
        request = parse_contract(encode(request.model_dump(mode="json")), ValidateMergeRevision)
        digest = self._identity(repo, request, actor, key)
        route = f"POST /merge-proposals/{proposal}/validate"
        with self.history.engine.begin() as connection:
            self._lock(connection, repo, actor, route, key)
            cached = self._cached(connection, "merge_validations", actor, route, key, digest)
            if cached is not None:
                _, record = self._validation(connection, repo, proposal, cached["id"])
                return dict(
                    validation_id=str(cached["id"]),
                    validation_ref=cached["validation_ref"],
                    record=record.model_dump(mode="json"),
                )
            root, manifest = self._root(connection, repo, proposal)
            self._open(connection, proposal)
            row, revision = self._revision(connection, repo, proposal)
            self._expected(row, request)
            self._pins(connection, repo, manifest)
            preview = self._preview(repo, manifest, revision.edits)
            if preview.validation.candidate_digest != revision.candidate_digest:
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "来源证据或候选条件已变化，请先追加修订"
                )
            identity = uuid4()
            record = MergeValidationRecord(
                proposal_ref=root["manifest_ref"],
                revision_ref=row["revision_ref"],
                result=preview.validation,
            )
            ref = self._store(connection, repo, identity, "merge_validation", "validation", record)
            connection.execute(
                TABLES["merge_validations"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    proposal_id=proposal,
                    revision_id=row["id"],
                    validation_ref=ref,
                    candidate_digest=revision.candidate_digest,
                    passed=record.result.validated,
                    actor=actor,
                    route=route,
                    request_key=key,
                    request_digest=digest,
                )
            )
            for ordinal, conflict in enumerate(record.result.conflicts):
                conflict_id = uuid4()
                conflict_ref = self._store(
                    connection, repo, conflict_id, "merge_conflict", "conflict", conflict
                )
                connection.execute(
                    TABLES["merge_conflicts"]
                    .insert()
                    .values(
                        id=conflict_id,
                        repository_id=repo,
                        proposal_id=proposal,
                        revision_id=row["id"],
                        validation_id=identity,
                        ordinal=ordinal,
                        kind=conflict.kind.value,
                        conflict_ref=conflict_ref,
                    )
                )
            return dict(
                validation_id=str(identity),
                validation_ref=ref,
                record=record.model_dump(mode="json"),
            )

    def review(
        self, repo: UUID, proposal: UUID, request: PrepareMergeReview, *, actor: str, key: str
    ) -> dict[str, Any]:
        request = parse_contract(encode(request.model_dump(mode="json")), PrepareMergeReview)
        digest = self._identity(repo, request, actor, key)
        route = f"POST /merge-proposals/{proposal}/review"
        with self.history.engine.begin() as connection:
            self._lock(connection, repo, actor, route, key)
            cached = self._cached(connection, "merge_review_sessions", actor, route, key, digest)
            if cached is not None:
                ticket = self._owned(
                    connection,
                    repo,
                    cached,
                    "ticket_ref",
                    "merge_review",
                    "ticket",
                    MergeReviewTicket,
                )
                return dict(
                    review_id=str(cached["id"]),
                    ticket_ref=cached["ticket_ref"],
                    ticket=ticket.model_dump(mode="json"),
                )
            root, manifest = self._root(connection, repo, proposal)
            self._open(connection, proposal)
            row, revision = self._revision(connection, repo, proposal)
            self._expected(row, request)
            validation, record = self._validation(
                connection, repo, proposal, UUID(request.validation_id)
            )
            if validation["revision_id"] != row["id"] or not record.result.validated:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "审阅需要当前修订已通过的精确验证")
            self._pins(connection, repo, manifest)
            fresh = self._preview(repo, manifest, revision.edits)
            if fresh.validation != record.result:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "合并验证条件已变化，请重新验证")
            identity = uuid4()
            ticket = MergeReviewTicket(
                proposal_ref=root["manifest_ref"],
                revision_ref=row["revision_ref"],
                validation_id=request.validation_id,
                validation_ref=validation["validation_ref"],
                candidate_digest=revision.candidate_digest,
                actor=actor,
                ours=manifest.request.ours,
                theirs=manifest.request.theirs,
            )
            ref = self._store(connection, repo, identity, "merge_review", "ticket", ticket)
            connection.execute(
                TABLES["merge_review_sessions"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    proposal_id=proposal,
                    revision_id=row["id"],
                    validation_id=validation["id"],
                    ticket_ref=ref,
                    actor=actor,
                    route=route,
                    request_key=key,
                    request_digest=digest,
                )
            )
            return dict(
                review_id=str(identity), ticket_ref=ref, ticket=ticket.model_dump(mode="json")
            )

    def reject(
        self, repo: UUID, proposal: UUID, request: RejectMergeProposal, *, actor: str, key: str
    ) -> dict[str, Any]:
        request = parse_contract(encode(request.model_dump(mode="json")), RejectMergeProposal)
        digest = self._identity(repo, request, actor, key)
        route = f"POST /merge-proposals/{proposal}/reject"
        with self.history.engine.begin() as connection:
            self._lock(connection, repo, actor, route, key)
            cached = self._cached(connection, "merge_rejections", actor, route, key, digest)
            if cached is not None:
                decision = self._owned(
                    connection,
                    repo,
                    cached,
                    "decision_ref",
                    "merge_rejection",
                    "decision",
                    MergeRejection,
                )
                return dict(
                    decision_id=str(cached["id"]),
                    decision_ref=cached["decision_ref"],
                    decision=decision.model_dump(mode="json"),
                )
            root, _ = self._root(connection, repo, proposal)
            self._open(connection, proposal)
            row, _ = self._revision(connection, repo, proposal)
            self._expected(row, request)
            identity = uuid4()
            decision = MergeRejection(
                proposal_ref=root["manifest_ref"],
                revision_ref=row["revision_ref"],
                actor=actor,
                reason=request.reason,
            )
            ref = self._store(connection, repo, identity, "merge_rejection", "decision", decision)
            connection.execute(
                TABLES["merge_rejections"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    proposal_id=proposal,
                    revision_id=row["id"],
                    decision_ref=ref,
                    actor=actor,
                    route=route,
                    request_key=key,
                    request_digest=digest,
                )
            )
            return dict(
                decision_id=str(identity),
                decision_ref=ref,
                decision=decision.model_dump(mode="json"),
            )

    def show(
        self,
        repo: UUID,
        proposal: UUID,
        *,
        review_before: UUID | None = None,
        review_id: UUID | None = None,
    ) -> dict[str, Any]:
        if review_before is not None and review_id is not None:
            raise AgentGitError(ErrorCode.VALIDATION, "票据 ID 与分页游标不能同时指定")
        with self.history.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            root, manifest = self._root(connection, repo, proposal)
            row, revision = self._revision(connection, repo, proposal)
            if revision.proposal_ref != root["manifest_ref"]:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并修订不属于确切提案")
            table = TABLES["merge_validations"]
            latest = (
                connection.execute(
                    sa.select(table)
                    .where(table.c.proposal_id == proposal, table.c.revision_id == row["id"])
                    .order_by(table.c.created_at.desc(), table.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            record = None
            conflicts: list[MergeConflict] = []
            if latest is not None:
                _, record = self._validation(connection, repo, proposal, latest["id"])
                table = TABLES["merge_conflicts"]
                for indexed in connection.execute(
                    sa.select(table)
                    .where(table.c.validation_id == latest["id"])
                    .order_by(table.c.ordinal)
                ).mappings():
                    conflict = self._owned(
                        connection,
                        repo,
                        indexed,
                        "conflict_ref",
                        "merge_conflict",
                        "conflict",
                        MergeConflict,
                    )
                    if conflict.kind.value != indexed["kind"] or indexed["ordinal"] != len(
                        conflicts
                    ):
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并冲突索引不完整")
                    conflicts.append(conflict)
                if tuple(conflicts) != record.result.conflicts:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并冲突与验证原文不同")
            table = TABLES["merge_rejections"]
            rejected = (
                connection.execute(sa.select(table).where(table.c.proposal_id == proposal))
                .mappings()
                .first()
            )
            decision = (
                self._owned(
                    connection,
                    repo,
                    rejected,
                    "decision_ref",
                    "merge_rejection",
                    "decision",
                    MergeRejection,
                )
                if rejected is not None
                else None
            )
            current = revision.code_revision == source_revision()
            for pin in (manifest.request.ours, manifest.request.theirs):
                branches = TABLES["branches"]
                branch = (
                    connection.execute(
                        sa.select(branches).where(
                            branches.c.repository_id == repo, branches.c.name == pin.name
                        )
                    )
                    .mappings()
                    .one()
                )
                current = (
                    current
                    and branch["head_commit_id"] == pin.head
                    and branch["version"] == pin.version
                    and branch["active_writer_run_id"] is None
                )
            status = (
                "rejected"
                if decision
                else (
                    "stale"
                    if not current
                    else (
                        "validated"
                        if record and record.result.validated
                        else ("needs_review" if record else "draft")
                    )
                )
            )
            table = TABLES["merge_revisions"]
            revisions = (
                connection.execute(
                    sa.select(table)
                    .where(table.c.proposal_id == proposal)
                    .order_by(table.c.number)
                    .limit(100)
                )
                .mappings()
                .all()
            )
            revision_index = {item["id"]: item for item in revisions}
            sessions, validations = TABLES["merge_review_sessions"], TABLES["merge_validations"]
            page = (
                sa.select(sessions, validations.c.validation_ref, validations.c.passed)
                .join(
                    validations,
                    sa.and_(
                        validations.c.repository_id == sessions.c.repository_id,
                        validations.c.proposal_id == sessions.c.proposal_id,
                        validations.c.revision_id == sessions.c.revision_id,
                        validations.c.id == sessions.c.validation_id,
                    ),
                )
                .where(sessions.c.repository_id == repo, sessions.c.proposal_id == proposal)
            )
            if review_id is not None:
                page = page.where(sessions.c.id == review_id)
            elif review_before is not None:
                cursor = (
                    connection.execute(
                        sa.select(sessions.c.created_at, sessions.c.id).where(
                            sessions.c.repository_id == repo,
                            sessions.c.proposal_id == proposal,
                            sessions.c.id == review_before,
                        )
                    )
                    .mappings()
                    .first()
                )
                if cursor is None:
                    raise AgentGitError(ErrorCode.NOT_FOUND, "合并审阅游标不可访问")
                page = page.where(
                    sa.or_(
                        sessions.c.created_at < cursor["created_at"],
                        sa.and_(
                            sessions.c.created_at == cursor["created_at"],
                            sessions.c.id < cursor["id"],
                        ),
                    )
                )
            sessions_rows = (
                connection.execute(
                    page.order_by(sessions.c.created_at.desc(), sessions.c.id.desc()).limit(101)
                )
                .mappings()
                .all()
            )
            reviews = []
            for session in sessions_rows[:100]:
                ticket = self._owned(
                    connection,
                    repo,
                    session,
                    "ticket_ref",
                    "merge_review",
                    "ticket",
                    MergeReviewTicket,
                )
                linked = revision_index.get(session["revision_id"])
                if (
                    linked is None
                    or ticket.actor != session["actor"]
                    or ticket.proposal_ref != root["manifest_ref"]
                    or ticket.ours != manifest.request.ours
                    or ticket.theirs != manifest.request.theirs
                    or ticket.revision_ref != linked["revision_ref"]
                    or ticket.candidate_digest != linked["candidate_digest"]
                    or ticket.validation_id != str(session["validation_id"])
                    or ticket.validation_ref != session["validation_ref"]
                    or not session["passed"]
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并审阅票据与索引或验证不符")
                reviews.append(
                    dict(
                        review_id=str(session["id"]),
                        ticket_ref=session["ticket_ref"],
                        ticket=ticket.model_dump(mode="json"),
                        current=current
                        and decision is None
                        and session["revision_id"] == row["id"],
                    )
                )
            return dict(
                proposal_id=str(proposal),
                proposal_ref=root["manifest_ref"],
                manifest=manifest.model_dump(mode="json"),
                status=status,
                revision=self._revision_receipt(row),
                candidate=revision.model_dump(mode="json"),
                validation_id=str(latest["id"]) if latest is not None else None,
                validation_ref=latest["validation_ref"] if latest is not None else None,
                validation=record.model_dump(mode="json") if record else None,
                requires_recheck=True,
                rejection=decision.model_dump(mode="json") if decision else None,
                revisions=[self._revision_receipt(item) for item in revisions],
                reviews=reviews,
                reviews_truncated=len(sessions_rows) > 100,
                reviews_next_cursor=str(sessions_rows[99]["id"])
                if len(sessions_rows) > 100
                else None,
            )

    def review_status(self, repo: UUID, proposal: UUID, review: UUID) -> dict[str, Any]:
        view = self.show(repo, proposal, review_id=review)
        if not view["reviews"]:
            raise AgentGitError(ErrorCode.NOT_FOUND, "合并审阅票据不可访问")
        return dict(view["reviews"][0])
