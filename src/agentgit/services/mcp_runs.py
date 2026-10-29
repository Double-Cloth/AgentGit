"""MCP 单次程序提交和完整录制选择，复用原 Job/Run/审批表。"""

import re
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Submission
from agentgit.core.mcp_runs import MCPJobPayload, MCPProgram, MCPRecordedSpec, MCPRunState
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.core.versions import RecordedRange
from agentgit.replay.recorded import Recording
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.versions import VersionService

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService


class MCPRunService:
    def __init__(self, queue: "JobService"):
        self.queue, self.history = queue, queue.history

    def payload(self, repository_id: UUID, job_id: UUID) -> MCPJobPayload:
        job = self.queue.get(repository_id, job_id)
        if job["kind"] != "mcp_run":
            raise AgentGitError(ErrorCode.FORBIDDEN, "Job 不是 MCP 程序")
        return parse_contract(
            self.history.objects.get(job["payload_ref"], ObjectType.BLOB), MCPJobPayload
        )

    def program(self, repository_id: UUID, payload: MCPJobPayload) -> MCPProgram:
        with self.history.engine.connect() as connection:
            refs = TABLES["object_refs"]
            exists = connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository_id,
                    refs.c.object_id == payload.program_ref,
                    refs.c.owner_type == "repository",
                    refs.c.role == "input",
                )
            ).first()
        if exists is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "MCP 程序输入缺少同项目登记")
        return parse_contract(
            self.history.objects.get(payload.program_ref, ObjectType.BLOB), MCPProgram
        )

    def submit(
        self,
        repository_id: UUID,
        program: MCPProgram,
        *,
        actor: str,
        key: str,
        branch_name: str,
        expected_head: str | None,
        expected_version: int,
        replay: MCPRecordedSpec | None = None,
    ) -> Submission:
        program = parse_contract(encode(program.model_dump(mode="json")), MCPProgram)
        if (
            type(expected_version) is not int
            or expected_version < 0
            or not 1 <= len(branch_name) <= 100
            or (expected_head is not None and re.fullmatch(r"[0-9a-f]{64}", expected_head) is None)
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "MCP 分支版本或 HEAD 格式无效")
        if not actor or not key or len(actor) > 200 or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "MCP 操作者与幂等键必须有界且非空")
        program_obj = self.history.objects.put(
            ObjectType.BLOB, encode(program.model_dump(mode="json"))
        )
        if replay is not None:
            replay = parse_contract(encode(replay.model_dump(mode="json")), MCPRecordedSpec)
            self.recording(repository_id, program_obj.id, replay)
        payload = MCPJobPayload(
            program_ref=program_obj.id,
            actor=actor,
            code_revision=source_revision(),
            dependency_lock_digest=dependency_lock_digest(),
            max_wall_ms=program.limits.max_wall_ms,
            replay=replay,
        )
        payload_obj = self.history.objects.put(
            ObjectType.BLOB, encode(payload.model_dump(mode="json"))
        )
        job_id, run_id = uuid4(), uuid4()
        submission = Submission(job_id=str(job_id), run_id=str(run_id))
        response = self.history.objects.put(
            ObjectType.BLOB, encode(submission.model_dump(mode="json"))
        )
        route = f"POST /repositories/{repository_id}/mcp-runs"
        request_digest = object_digest(
            ObjectType.BLOB,
            encode(
                {
                    "schema_version": 1,
                    "program_ref": program_obj.id,
                    "branch_name": branch_name,
                    "head": expected_head,
                    "version": expected_version,
                    "replay": replay.model_dump(mode="json") if replay is not None else None,
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
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "MCP 原键不能绑定不同输入")
                return parse_contract(
                    self.history.objects.get(cached["result_ref"], ObjectType.BLOB), Submission
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
                raise AgentGitError(ErrorCode.NOT_FOUND, "MCP 分支不可访问")
            if (
                branch["head_commit_id"] != expected_head
                or branch["version"] != expected_version
                or branch["active_writer_run_id"] is not None
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "MCP 分支 HEAD、版本或写入者已变化")
            if replay is not None:
                forks = TABLES["branch_forks"]
                fork = (
                    connection.execute(
                        sa.select(forks).where(
                            forks.c.repository_id == repository_id,
                            forks.c.branch_name == branch_name,
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    fork is None
                    or str(fork["source_run_id"]) != replay.source_run_id
                    or fork["from_commit_id"] != replay.from_commit
                    or expected_head != replay.from_commit
                ):
                    raise AgentGitError(
                        ErrorCode.FORBIDDEN, "MCP Recorded 必须绑定明确的独立来源分支"
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
                    origin_run_id=UUID(replay.source_run_id) if replay else None,
                    mode="recorded" if replay else "live",
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
                    origin_run_id=UUID(replay.source_run_id) if replay else None,
                    kind="mcp_run",
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

    def recording(
        self, repository_id: UUID, program_ref: str, replay: MCPRecordedSpec
    ) -> Recording:
        source_id = UUID(replay.source_run_id)
        with self.history.engine.connect() as connection:
            jobs, calls = TABLES["jobs"], TABLES["external_calls"]
            job = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.repository_id == repository_id, jobs.c.run_id == source_id
                    )
                )
                .mappings()
                .first()
            )
            if job is None or job["kind"] != "mcp_run":
                raise AgentGitError(ErrorCode.NOT_FOUND, "MCP 录制来源不可访问")
            rows = list(
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository_id, calls.c.run_id == source_id
                    )
                ).mappings()
            )
        payload = self.payload(repository_id, job["id"])
        program = self.program(repository_id, payload)
        if (
            payload.program_ref != program_ref
            or payload.code_revision != source_revision()
            or payload.dependency_lock_digest != dependency_lock_digest()
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "MCP 录制输入或环境与当前不符")
        VersionService(self.history).select_range(
            repository_id,
            RecordedRange(
                source_run_id=replay.source_run_id,
                from_commit=replay.from_commit,
                until_commit=replay.to_commit,
            ),
        )
        before = parse_contract(
            encode(self.history.restore(repository_id, replay.from_commit).data), MCPRunState
        )
        after = parse_contract(
            encode(self.history.restore(repository_id, replay.to_commit).data), MCPRunState
        )
        if (
            before.program_ref != program_ref
            or after.program_ref != program_ref
            or not before.pending
            or before.response_ref is not None
            or after.status.value != "completed"
            or after.pending
            or len(rows) != 1
        ):
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "MCP 来源缺少明确的单次成功范围")
        row = rows[0]
        if row["status"] != "succeeded" or row["result_ref"] is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "MCP 来源没有完整成功录制")
        intent = parse_contract(
            self.history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent
        )
        saved = parse_contract(
            self.history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
        )
        request = self.history.objects.get(intent.request_ref, ObjectType.BLOB)
        response = self.history.objects.get(saved.response_ref, ObjectType.BLOB)
        if (
            intent.checkpoint != replay.from_commit
            or not saved.complete
            or saved.error is not None
            or after.response_ref != saved.response_ref
            or intent.effect != program.binding.effect_class
            or intent.occurrence.provider != program.binding.provider
            or intent.occurrence.signature_digest != program.binding.signature()
            or intent.occurrence.signature_digest != row["signature_digest"]
            or intent.occurrence.provider != row["provider"]
            or intent.effect.value != row["effect"]
            or intent.occurrence.request_digest != row["request_digest"]
            or saved.response_bytes != len(response)
            or saved.response_bytes > intent.max_response_bytes
            or request != encode(program.request.model_dump(mode="json"))
            or intent.max_response_bytes != program.binding.server.max_response_bytes
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "MCP 录制与固定程序、来源或回执不符")
        snapshot = self.history.restore(repository_id, replay.to_commit)
        if (
            len(snapshot.artifacts) != 1
            or snapshot.artifacts[0].path != "mcp-result.json"
            or snapshot.artifacts[0].object_ref != saved.response_ref
            or snapshot.artifacts[0].media_type != "application/json"
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "MCP 原始产物与成功响应不符")
        return Recording(intent, saved, request, response, str(row["id"]))
