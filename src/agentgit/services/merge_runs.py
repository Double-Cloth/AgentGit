"""已批准合并的独立分支和新任务，复用 Job/Worker 的预算与审批边界。"""

from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge_proposals import MergeApproval, MergeArtifactDocument
from agentgit.core.merge_runs import ForkMergedBranch, MergedRunSpec, NewMergedRun
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.experiments.validation import frozen_inputs
from agentgit.merge.initialize import initialize_merged
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.merge_proposals import MergeProposalService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.roles import path_reference_role


def approved_snapshot(
    history: HistoryService,
    connection: Connection,
    repository: UUID,
    approval_id: UUID,
    commit: str,
) -> StateSnapshot:
    table = TABLES["merge_approvals"]
    row = (
        connection.execute(
            sa.select(table).where(
                table.c.repository_id == repository,
                table.c.id == approval_id,
                table.c.commit_id == commit,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise AgentGitError(ErrorCode.NOT_FOUND, "已批准的合并来源不可访问")
    service = MergeProposalService(history)
    receipt = service._approval(connection, repository, row)
    decision = parse_contract(encode(receipt["decision"]), MergeApproval)
    _, candidate = service._revision(connection, repository, row["proposal_id"], row["revision_id"])
    snapshot = history.restore(repository, commit)
    if (
        candidate.state is None
        or snapshot.data != candidate.state.model_dump(mode="json")
        or decision.candidate_digest != candidate.candidate_digest
    ):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并快照与批准候选不符")
    documents: dict[str, MergeArtifactDocument] = {
        item.path: item for item in candidate.artifacts if item.text is not None
    }
    if {item.path for item in snapshot.artifacts} != set(documents):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并产物集合与批准不同")
    for item in snapshot.artifacts:
        document = documents[item.path]
        if (
            item.media_type != document.media_type
            or history.objects.get(item.object_ref, ObjectType.BLOB)
            != (document.text or "").encode()
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并产物字节与批准不同")
    return snapshot


class MergeRunService:
    def __init__(self, research: ResearchService):
        self.research, self.history = research, research.history

    @staticmethod
    def _lock_repository(connection: Connection, repository: UUID) -> None:
        table = TABLES["repositories"]
        if (
            connection.execute(
                sa.select(table.c.id).where(table.c.id == repository).with_for_update()
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")

    def fork(
        self, repository: UUID, request: ForkMergedBranch, *, actor: str, connection: Connection
    ) -> dict[str, Any]:
        self._lock_repository(connection, repository)
        request = parse_contract(encode(request.model_dump(mode="json")), ForkMergedBranch)
        approved_snapshot(
            self.history, connection, repository, UUID(request.approval_id), request.commit_id
        )
        branches = TABLES["branches"]
        if (
            connection.execute(
                sa.select(branches.c.name).where(
                    branches.c.repository_id == repository,
                    branches.c.name == request.name,
                )
            ).first()
            is not None
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "分支名已存在")
        connection.execute(
            branches.insert().values(
                repository_id=repository,
                name=request.name,
                head_commit_id=request.commit_id,
                version=0,
            )
        )
        identity = uuid4()
        obj = self.history.objects.put_json(ObjectType.BLOB, request.model_dump(mode="json"))
        self.history._ref(connection, repository, obj, "merge_branch", str(identity), "request")
        connection.execute(
            TABLES["merge_branch_origins"]
            .insert()
            .values(
                id=identity,
                repository_id=repository,
                branch_name=request.name,
                approval_id=UUID(request.approval_id),
                commit_id=request.commit_id,
                request_ref=obj.id,
                actor=actor,
            )
        )
        return dict(
            repository_id=str(repository),
            name=request.name,
            version=0,
            head_commit_id=request.commit_id,
            approval_id=request.approval_id,
            origin_kind="merge",
            execution="requires_new_run",
        )

    def start(
        self,
        repository: UUID,
        request: NewMergedRun,
        *,
        actor: str,
        key: str,
        connection: Connection,
    ) -> dict[str, Any]:
        self._lock_repository(connection, repository)
        request = parse_contract(encode(request.model_dump(mode="json")), NewMergedRun)
        branches, origins = TABLES["branches"], TABLES["merge_branch_origins"]
        branch = (
            connection.execute(
                sa.select(branches)
                .where(
                    branches.c.repository_id == repository,
                    branches.c.name == request.branch_name,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if branch is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "合并分支不可访问")
        if (
            branch["head_commit_id"] != request.expected_head
            or branch["version"] != request.expected_branch_version
            or branch["active_writer_run_id"] is not None
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "合并分支已经变化或被占用")
        origin = (
            connection.execute(
                sa.select(origins).where(
                    origins.c.repository_id == repository,
                    origins.c.branch_name == request.branch_name,
                    origins.c.commit_id == request.expected_head,
                )
            )
            .mappings()
            .first()
        )
        if origin is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "新任务需要绑定已批准合并的独立分支")
        requests = TABLES["merge_run_requests"]
        if (
            connection.execute(
                sa.select(requests.c.id).where(
                    requests.c.repository_id == repository,
                    requests.c.branch_name == request.branch_name,
                )
            ).first()
            is not None
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "该合并分支已提交新任务，请原键查询")
        original = approved_snapshot(
            self.history, connection, repository, origin["approval_id"], origin["commit_id"]
        )
        configs = TABLES["agent_configs"]
        config_row = (
            connection.execute(
                sa.select(configs)
                .where(
                    configs.c.repository_id == repository,
                    configs.c.id == UUID(request.config_id),
                )
                .with_for_update(read=True)
            )
            .mappings()
            .first()
        )
        if config_row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "新任务配置不可访问")
        if config_row["version"] != request.config_version:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "新任务配置版本已变化")
        config = self.research.config(repository, UUID(request.config_id), connection=connection)
        if not request.budget.within(config.limits):
            raise AgentGitError(ErrorCode.FORBIDDEN, "新任务预算不能扩大配置上限")
        config = evolve(config, limits=request.budget)
        validator = ValidatorService(self.history).manifest(repository, request.validator_ref)
        goal, corpus = frozen_inputs(self.history.objects, validator)
        snapshot = initialize_merged(original, config, corpus, self.history.objects.get)
        if snapshot.data["goal"] != goal.model_dump(mode="json"):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "新验收器目标与合并任务不同")
        state_obj = self.history.objects.put_json(
            ObjectType.STATE, snapshot.model_dump(mode="json")
        )
        research = ResearchService(self.history, self.research.data_root, corpus=corpus)
        task = research.create_task(
            repository, UUID(request.config_id), goal, connection=connection
        )
        spec = MergedRunSpec(
            approval_id=str(origin["approval_id"]),
            from_commit=request.expected_head,
            initial_state_ref=state_obj.id,
            validator_ref=request.validator_ref,
        )
        submitted = JobService(research).enqueue(
            repository,
            task,
            actor=actor,
            key=key,
            config_version=request.config_version,
            expected_branch_version=request.expected_branch_version,
            expected_head=request.expected_head,
            branch_name=request.branch_name,
            budget=request.budget,
            merged=spec,
            connection=connection,
        )
        self.history._ref(
            connection, repository, state_obj, "merge_run_input", submitted.job_id, "state"
        )
        for artifact in snapshot.artifacts:
            obj = self.history.objects.put(
                ObjectType.BLOB, self.history.objects.get(artifact.object_ref, ObjectType.BLOB)
            )
            self.history._ref(
                connection,
                repository,
                obj,
                "merge_run_input",
                submitted.job_id,
                path_reference_role("file", artifact.path),
            )
        obj = self.history.objects.put_json(ObjectType.BLOB, request.model_dump(mode="json"))
        self.history._ref(
            connection, repository, obj, "merge_run_request", submitted.job_id, "request"
        )
        connection.execute(
            TABLES["merge_run_requests"]
            .insert()
            .values(
                id=UUID(submitted.job_id),
                job_id=UUID(submitted.job_id),
                repository_id=repository,
                branch_name=request.branch_name,
                approval_id=origin["approval_id"],
                commit_id=spec.from_commit,
                config_id=UUID(request.config_id),
                config_version=request.config_version,
                validator_ref=request.validator_ref,
                initial_state_ref=state_obj.id,
                request_ref=obj.id,
                actor=actor,
            )
        )
        self.history.fault("merge.run_stored")
        return dict(
            repository_id=str(repository),
            task_id=str(task),
            job_id=submitted.job_id,
            run_id=submitted.run_id,
            branch_name=request.branch_name,
            origin_kind="merge",
            from_commit=request.expected_head,
            approval_id=spec.approval_id,
            mode="live",
        )


def load_merged_input(
    history: HistoryService,
    repository: UUID,
    job: UUID,
    spec: MergedRunSpec,
    config: AgentConfig,
    corpus: Corpus,
) -> StateSnapshot:
    with history.engine.connect() as connection:
        table = TABLES["merge_run_requests"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repository,
                    table.c.job_id == job,
                )
            )
            .mappings()
            .first()
        )
        if row is None or any(
            row[field] != value
            for field, value in (
                ("approval_id", UUID(spec.approval_id)),
                ("commit_id", spec.from_commit),
                ("initial_state_ref", spec.initial_state_ref),
                ("validator_ref", spec.validator_ref),
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Job 缺少精确合并输入记录")
        service = MergeProposalService(history)
        request = service._owned(
            connection, repository, row, "request_ref", "merge_run_request", "request", NewMergedRun
        )
        if (
            request.expected_head != spec.from_commit
            or request.validator_ref != spec.validator_ref
            or UUID(request.config_id) != row["config_id"]
            or request.config_version != row["config_version"]
            or request.branch_name != row["branch_name"]
            or request.budget != config.limits
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并任务请求与 Job 或配置不符")
        original = approved_snapshot(
            history, connection, repository, UUID(spec.approval_id), spec.from_commit
        )
        refs = TABLES["object_refs"]
        owned = set(
            connection.execute(
                sa.select(refs.c.role, refs.c.object_id).where(
                    refs.c.repository_id == repository,
                    refs.c.owner_type == "merge_run_input",
                    refs.c.owner_id == str(job),
                )
            ).tuples()
        )
    expected = initialize_merged(original, config, corpus, history.objects.get)
    validator = ValidatorService(history).manifest(repository, spec.validator_ref)
    goal, frozen_corpus = frozen_inputs(history.objects, validator)
    if corpus.digest() != frozen_corpus.digest() or expected.data["goal"] != goal.model_dump(
        mode="json"
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "合并 Job 的冻结验收输入不符")
    actual = parse_contract(
        history.objects.get(spec.initial_state_ref, ObjectType.STATE), StateSnapshot
    )
    required = {("state", spec.initial_state_ref)} | {
        (path_reference_role("file", item.path), item.object_ref) for item in actual.artifacts
    }
    if actual != expected or not required <= owned:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并 Job 的初始状态或文件归属不符")
    return actual
