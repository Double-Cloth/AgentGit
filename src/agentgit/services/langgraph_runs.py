"""固定图提交：原子绑定分支、冻结输入并复用既有幂等 Job。"""

import re
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Submission
from agentgit.core.langgraph import LangGraphProgram
from agentgit.core.langgraph_runs import LangGraphForkSpec, LangGraphJobPayload, NewLangGraphFork
from agentgit.core.models import parse_contract
from agentgit.core.versions import NewBranch
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.langgraph_forks import load_fork_source
from agentgit.services.versions import PreparedBranch, VersionService

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService


class LangGraphRunService:
    def __init__(self, queue: "JobService"):
        self.queue, self.history = queue, queue.history

    def payload(self, repository_id: UUID, job_id: UUID) -> LangGraphJobPayload:
        job = self.queue.get(repository_id, job_id)
        if job["kind"] != "langgraph_run":
            raise AgentGitError(ErrorCode.FORBIDDEN, "Job 不是固定 LangGraph 程序")
        return parse_contract(
            self.history.objects.get(job["payload_ref"], ObjectType.BLOB), LangGraphJobPayload
        )

    def program(self, repository_id: UUID, payload: LangGraphJobPayload) -> LangGraphProgram:
        refs = TABLES["object_refs"]
        with self.history.engine.connect() as connection:
            own = connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository_id,
                    refs.c.object_id == payload.program_ref,
                    refs.c.owner_type == "repository",
                    refs.c.role == "input",
                )
            ).first()
        if own is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "LangGraph 程序缺少同项目输入登记")
        return parse_contract(
            self.history.objects.get(payload.program_ref, ObjectType.BLOB), LangGraphProgram
        )

    def submit(
        self,
        repository_id: UUID,
        program: LangGraphProgram,
        *,
        actor: str,
        key: str,
        branch_name: str,
        expected_head: str | None,
        expected_version: int,
    ) -> Submission:
        return self._submit(
            repository_id,
            program,
            actor=actor,
            key=key,
            branch_name=branch_name,
            expected_head=expected_head,
            expected_version=expected_version,
        )

    def fork(
        self, repository_id: UUID, request: NewLangGraphFork, *, actor: str, key: str
    ) -> Submission:
        request = parse_contract(encode(request.model_dump(mode="json")), NewLangGraphFork)
        source, state, _ = load_fork_source(
            self.history, repository_id, UUID(request.source_run_id), request.from_commit
        )
        assert state.native_ref is not None
        spec = LangGraphForkSpec(
            source_run_id=request.source_run_id,
            from_commit=request.from_commit,
            source_program_ref=state.program_ref,
            native_ref=state.native_ref,
        )
        program = parse_contract(
            encode({**source.model_dump(mode="json"), "review_note": request.review_note}),
            LangGraphProgram,
        )
        branch = NewBranch(
            source_run_id=request.source_run_id,
            from_commit=request.from_commit,
            name=request.name,
            expected_version=request.expected_version,
        )
        prepared = VersionService(self.history).prepare_fork(repository_id, branch)
        return self._submit(
            repository_id,
            program,
            actor=actor,
            key=key,
            branch_name=request.name,
            expected_head=request.from_commit,
            expected_version=0,
            fork=spec,
            prepared=prepared,
        )

    def _submit(
        self,
        repository_id: UUID,
        program: LangGraphProgram,
        *,
        actor: str,
        key: str,
        branch_name: str,
        expected_head: str | None,
        expected_version: int,
        fork: LangGraphForkSpec | None = None,
        prepared: PreparedBranch | None = None,
    ) -> Submission:
        program = parse_contract(encode(program.model_dump(mode="json")), LangGraphProgram)
        if (
            type(expected_version) is not int
            or expected_version < 0
            or not 1 <= len(branch_name) <= 100
            or (expected_head is not None and re.fullmatch(r"[0-9a-f]{64}", expected_head) is None)
            or not 1 <= len(actor) <= 200
            or not 1 <= len(key) <= 200
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "LangGraph 分支、版本或操作身份无效")
        program_obj = self.history.objects.put(
            ObjectType.BLOB, encode(program.model_dump(mode="json"))
        )
        payload = LangGraphJobPayload(
            program_ref=program_obj.id,
            actor=actor,
            code_revision=source_revision(),
            dependency_lock_digest=dependency_lock_digest(),
            max_wall_ms=program.limits.max_wall_ms,
            fork=fork,
        )
        payload_obj = self.history.objects.put(
            ObjectType.BLOB, encode(payload.model_dump(mode="json"))
        )
        job_id, run_id = uuid4(), uuid4()
        submission = Submission(job_id=str(job_id), run_id=str(run_id))
        response = self.history.objects.put(
            ObjectType.BLOB, encode(submission.model_dump(mode="json"))
        )
        route = f"POST /repositories/{repository_id}/langgraph-{'forks' if fork else 'runs'}"
        request_digest = object_digest(
            ObjectType.BLOB,
            encode(
                {
                    "schema_version": 1,
                    "program_ref": program_obj.id,
                    "branch_name": branch_name,
                    "head": expected_head,
                    "version": expected_version,
                    **({"fork": fork.model_dump(mode="json")} if fork is not None else {}),
                }
            ),
        )
        with self.history.engine.begin() as connection:
            scope = encode([actor, route, key]).decode()
            connection.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(scope, 0)))
            )
            idem = TABLES["idempotency_keys"]
            cached = (
                connection.execute(
                    sa.select(idem).where(
                        idem.c.actor == actor, idem.c.route == route, idem.c.key == key
                    )
                )
                .mappings()
                .first()
            )
            if cached is not None:
                if (
                    cached["request_digest"] != request_digest
                    or cached["repository_id"] != repository_id
                ):
                    raise AgentGitError(
                        ErrorCode.VERSION_CONFLICT, "LangGraph 原键不能绑定不同输入"
                    )
                return parse_contract(
                    self.history.objects.get(cached["result_ref"], ObjectType.BLOB), Submission
                )
            if prepared is not None:
                VersionService(self.history).publish_fork(
                    prepared, actor=actor, connection=connection
                )
            branches = TABLES["branches"]
            branch = (
                connection.execute(
                    sa.select(branches)
                    .where(
                        branches.c.repository_id == repository_id, branches.c.name == branch_name
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if branch is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "LangGraph 分支不可访问")
            if (
                branch["head_commit_id"] != expected_head
                or branch["version"] != expected_version
                or branch["active_writer_run_id"] is not None
            ):
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "LangGraph 分支 HEAD、版本或写入者已变化"
                )
            connection.execute(
                TABLES["runs"]
                .insert()
                .values(
                    id=run_id,
                    repository_id=repository_id,
                    branch_name=branch_name,
                    base_commit_id=expected_head,
                    checkpoint_commit_id=expected_head,
                    origin_run_id=UUID(fork.source_run_id) if fork else None,
                    mode="live",
                    status="queued",
                )
            )
            self.history._ref(
                connection, repository_id, program_obj, "repository", str(repository_id), "input"
            )
            self.history._ref(connection, repository_id, payload_obj, "job", str(job_id), "payload")
            self.history._ref(
                connection, repository_id, response, "idempotency", str(job_id), "response"
            )
            connection.execute(
                TABLES["jobs"]
                .insert()
                .values(
                    id=job_id,
                    repository_id=repository_id,
                    run_id=run_id,
                    origin_run_id=UUID(fork.source_run_id) if fork else None,
                    kind="langgraph_run",
                    payload_ref=payload_obj.id,
                )
            )
            connection.execute(
                idem.insert().values(
                    actor=actor,
                    route=route,
                    key=key,
                    repository_id=repository_id,
                    request_digest=request_digest,
                    result_ref=response.id,
                    job_id=job_id,
                )
            )
        return submission
