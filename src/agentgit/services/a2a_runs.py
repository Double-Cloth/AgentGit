"""A2A 程序提交与本 Run 回执归属，复用既有持久队列和调用账本。"""

import os
import re
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.a2a import check_a2a_content
from agentgit.core.a2a_runs import (
    A2AJobPayload,
    A2AProgram,
    A2ARecordedRunState,
    A2ARecordedSpec,
    A2ARunState,
)
from agentgit.core.a2a_wire import A2ACall, A2AResult, binding_digest
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Submission
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService


def check_program_secrets(program: A2AProgram) -> None:
    secret = os.environ.get(program.binding.server.credential_env) or None
    try:
        check_a2a_content(program.model_dump(mode="json"), secret)
        for raw in program.messages:
            check_a2a_content(A2ACall(operation="send", body_json=raw).body(), secret)
    except ValueError:
        raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 程序不能持久保存凭据内容") from None


class A2ARunService:
    def __init__(self, queue: "JobService"):
        self.queue, self.history = queue, queue.history

    def payload(self, repository_id: UUID, job_id: UUID) -> A2AJobPayload:
        job = self.queue.get(repository_id, job_id)
        if job["kind"] != "a2a_run":
            raise AgentGitError(ErrorCode.FORBIDDEN, "Job 不是 A2A 程序")
        return parse_contract(
            self.history.objects.get(job["payload_ref"], ObjectType.BLOB), A2AJobPayload
        )

    def program(self, repository_id: UUID, payload: A2AJobPayload) -> A2AProgram:
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
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 程序缺少同项目输入登记")
        return parse_contract(
            self.history.objects.get(payload.program_ref, ObjectType.BLOB), A2AProgram
        )

    def submit(
        self,
        repository_id: UUID,
        program: A2AProgram,
        *,
        actor: str,
        key: str,
        branch_name: str,
        expected_head: str | None,
        expected_version: int,
        replay: A2ARecordedSpec | None = None,
    ) -> Submission:
        program = parse_contract(encode(program.model_dump(mode="json")), A2AProgram)
        check_program_secrets(program)
        if (
            type(expected_version) is not int
            or expected_version < 0
            or not 1 <= len(branch_name) <= 100
            or (expected_head is not None and re.fullmatch(r"[0-9a-f]{64}", expected_head) is None)
            or not 1 <= len(actor) <= 200
            or not 1 <= len(key) <= 200
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "A2A 提交的版本、HEAD、操作者或原键无效")
        if replay is not None:
            from agentgit.services.a2a_recordings import A2ARecordingReader

            replay = parse_contract(encode(replay.model_dump(mode="json")), A2ARecordedSpec)
            source = A2ARecordingReader(self.history).inspect(repository_id, replay, actor=actor)
            if source.program != program:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "A2A Recorded 程序与明确来源不符")
        program_obj = self.history.objects.put(
            ObjectType.BLOB, encode(program.model_dump(mode="json"))
        )
        payload = A2AJobPayload(
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
        route = f"POST /repositories/{repository_id}/a2a-runs"
        request_digest = object_digest(
            ObjectType.BLOB,
            encode(
                dict(
                    schema_version=1,
                    program_ref=program_obj.id,
                    branch_name=branch_name,
                    head=expected_head,
                    version=expected_version,
                    **({"replay": replay.model_dump(mode="json")} if replay else {}),
                )
            ),
        )
        with self.history.engine.begin() as connection:
            scope = encode([actor, route, key]).decode()
            connection.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(scope, 0)))
            )
            idem, branches = TABLES["idempotency_keys"], TABLES["branches"]
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
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "A2A 原键不能绑定不同输入")
                return parse_contract(
                    self.history.objects.get(cached["result_ref"], ObjectType.BLOB), Submission
                )
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
                raise AgentGitError(ErrorCode.NOT_FOUND, "A2A 分支不可访问")
            if (
                branch["head_commit_id"] != expected_head
                or branch["version"] != expected_version
                or branch["active_writer_run_id"] is not None
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "A2A 分支 HEAD、版本或写入者已变化")
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
                        ErrorCode.FORBIDDEN, "A2A Recorded 必须绑定明确的独立来源分支"
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
                    mode="recorded" if replay else "live",
                    origin_run_id=UUID(replay.source_run_id) if replay else None,
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
                    kind="a2a_run",
                    origin_run_id=UUID(replay.source_run_id) if replay else None,
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

    def owned(self, repository_id: UUID, run_id: UUID, actor: str) -> A2AJobPayload:
        self.queue.run(repository_id, run_id)
        with self.history.engine.connect() as connection:
            jobs = TABLES["jobs"]
            job = connection.execute(
                sa.select(jobs.c.id).where(
                    jobs.c.repository_id == repository_id,
                    jobs.c.run_id == run_id,
                    jobs.c.kind == "a2a_run",
                )
            ).scalar_one_or_none()
        if job is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "A2A Run 不可访问")
        payload = self.payload(repository_id, job)
        if payload.actor != actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A Run 不属于当前操作者")
        return payload

    def state(self, repository_id: UUID, run_id: UUID, *, actor: str) -> A2ARunState | None:
        payload = self.owned(repository_id, run_id, actor)
        if payload.replay is not None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 状态须读取独立本地／逻辑投影")
        run = self.queue.run(repository_id, run_id)
        with self.history.engine.connect() as connection:
            snapshots = TABLES["snapshots"]
            own = connection.execute(
                sa.select(snapshots.c.commit_id).where(
                    snapshots.c.run_id == run_id,
                    snapshots.c.commit_id == run["checkpoint_commit_id"],
                )
            ).first()
        if own is None:
            return None
        state = parse_contract(
            encode(self.history.restore(repository_id, run["checkpoint_commit_id"]).data),
            A2ARunState,
        )
        if state.program_ref != payload.program_ref:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 状态与本 Run 程序不符")
        return state

    def recorded_state(
        self, repository_id: UUID, run_id: UUID, *, actor: str
    ) -> A2ARecordedRunState | None:
        payload = self.owned(repository_id, run_id, actor)
        if payload.replay is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "此Run没有Recorded来源")
        run = self.queue.run(repository_id, run_id)
        snapshots = TABLES["snapshots"]
        with self.history.engine.connect() as c:
            own = c.execute(
                sa.select(snapshots.c.commit_id).where(
                    snapshots.c.repository_id == repository_id,
                    snapshots.c.run_id == run_id,
                    snapshots.c.commit_id == run["checkpoint_commit_id"],
                )
            ).first()
        if own is None:
            return None
        state = parse_contract(
            encode(self.history.restore(repository_id, run["checkpoint_commit_id"]).data),
            A2ARecordedRunState,
        )
        if state.program_ref != payload.program_ref or state.replay != payload.replay:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded检查点来源或程序不符")
        return state

    def confirmed_result(
        self, repository_id: UUID, run_id: UUID, response_ref: str, program: A2AProgram
    ) -> A2AResult:
        with self.history.engine.connect() as connection:
            calls = TABLES["external_calls"]
            rows = (
                connection.execute(
                    sa.select(calls)
                    .where(
                        calls.c.repository_id == repository_id,
                        calls.c.run_id == run_id,
                        calls.c.status == "succeeded",
                    )
                    .limit(65)
                )
                .mappings()
                .all()
            )
        matched = []
        for row in rows:
            saved = parse_contract(
                self.history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
            )
            if saved.response_ref == response_ref:
                matched.append((row, saved))
        if len(matched) != 1:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 回执没有唯一的本 Run 已确认调用")
        row, saved = matched[0]
        raw = self.history.objects.get(response_ref, ObjectType.BLOB)
        result = parse_contract(raw, A2AResult)
        if (
            not saved.complete
            or saved.error is not None
            or saved.response_bytes != len(raw)
            or result.binding_ref != binding_digest(program.binding)
            or result.request_ref != row["request_digest"]
            or row["provider"] != program.binding.provider(result.operation)
            or row["signature_digest"] != program.binding.signature(result.operation)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 回执签名、请求或完整性不符")
        intent = parse_contract(
            self.history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent
        )
        if (
            intent.occurrence.request_digest != result.request_ref
            or intent.effect != program.binding.effect(result.operation)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 回执与原始意图不符")
        return result
