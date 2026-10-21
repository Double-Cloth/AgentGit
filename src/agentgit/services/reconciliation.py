"""可信 Adapter 查询结果，短事务附着证据；没有可信肯定回执即保持未知。"""

from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.core.snapshots import EventPayload
from agentgit.runtime.ports import OutcomeResolver
from agentgit.services.history import TABLES, EventInput, HistoryService


class ReconciliationService:
    def __init__(self, history: HistoryService):
        self.history = history

    def confirm(
        self,
        repository_id: UUID,
        run_id: UUID,
        call_id: UUID,
        *,
        actor: str,
        expected_version: int,
        resolver: OutcomeResolver,
    ) -> dict[str, Any] | None:
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "核对操作者无效")
        with self.history.engine.begin() as connection:
            run = self.history._run(connection, repository_id, run_id, lock=True)
            cached = self._cached(connection, repository_id, run_id, call_id, actor, resolver)
            if cached is not None:
                return cached
            call, _ = self._context(connection, run, call_id, actor, expected_version, resolver)
            checkpoint = run["checkpoint_commit_id"]
            request = self.history.objects.get(call["request_digest"], ObjectType.BLOB)
            intent = parse_contract(
                self.history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
            )
            occurrence = intent.occurrence
            if any(
                occurrence.model_dump()[key] != call[key]
                for key in (
                    "logical_step_id",
                    "call_index",
                    "attempt",
                    "provider",
                    "signature_digest",
                    "request_digest",
                )
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对意图与调用身份不符")
            if (
                intent.request_ref != call["request_digest"]
                or intent.effect.value != call["effect"]
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对请求或效果分类不符")
        # 此处没有数据库事务或租约；查询只读外部结果，不发送原写动作。
        verified = resolver.resolve(request)
        if verified is None:
            return None
        if (
            not isinstance(verified.payload, bytes)
            or not isinstance(verified.evidence, bytes)
            or not verified.evidence
            or len(verified.payload) > intent.max_response_bytes
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "核对结果缺少完整证据或超过原响应上限")
        response = self.history.objects.put(ObjectType.BLOB, verified.payload)
        evidence = self.history.objects.put(ObjectType.BLOB, verified.evidence)
        result = self.history.objects.put(
            ObjectType.CALL,
            encode(
                SavedCallOutcome(
                    response_ref=response.id,
                    response_bytes=len(verified.payload),
                    complete=True,
                ).model_dump(mode="json")
            ),
        )
        resolution_id = uuid4()
        table, calls, jobs = TABLES["call_resolutions"], TABLES["external_calls"], TABLES["jobs"]
        with self.history.engine.begin() as connection:
            run = self.history._run(connection, repository_id, run_id, lock=True)
            cached = self._cached(connection, repository_id, run_id, call_id, actor, resolver)
            if cached is not None:
                return cached
            current, job = self._context(
                connection, run, call_id, actor, expected_version, resolver
            )
            if (
                run["checkpoint_commit_id"] != checkpoint
                or current["intent_ref"] != call["intent_ref"]
                or current["request_digest"] != call["request_digest"]
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "查询期间核对目标已改变")
            for role, item in (("response", response), ("result", result), ("evidence", evidence)):
                self.history._ref(
                    connection, repository_id, item, "call_resolution", str(resolution_id), role
                )
            connection.execute(
                table.insert().values(
                    id=resolution_id,
                    repository_id=repository_id,
                    run_id=run_id,
                    call_id=call_id,
                    checkpoint_commit_id=checkpoint,
                    actor=actor,
                    resolver_signature=resolver.signature,
                    request_digest=call["request_digest"],
                    result_ref=result.id,
                    evidence_ref=evidence.id,
                )
            )
            connection.execute(
                calls.update()
                .where(calls.c.id == call_id)
                .values(
                    status="succeeded",
                    result_ref=result.id,
                )
            )
            event = EventInput(
                "call.reconciled",
                call["logical_step_id"],
                {
                    "resolution_id": str(resolution_id),
                    "origin_call_id": str(call_id),
                    "actor": actor,
                    "result_ref": result.id,
                    "evidence_ref": evidence.id,
                },
            )
            payload = self.history.objects.put_json(
                ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
            )
            seq = int(run["last_event_seq"]) + 1
            self.history._event(connection, repository_id, run_id, seq, event, payload)
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == run_id)
                .values(
                    last_event_seq=seq,
                )
            )
            if job is not None:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == job["id"])
                    .values(
                        status="queued",
                        worker=None,
                        lease_until=None,
                        available_at=sa.func.clock_timestamp(),
                    )
                )
            return dict(
                connection.execute(sa.select(table).where(table.c.id == resolution_id))
                .mappings()
                .one()
            )

    def _cached(
        self,
        connection: Connection,
        repository_id: UUID,
        run_id: UUID,
        call_id: UUID,
        actor: str,
        resolver: OutcomeResolver,
    ) -> dict[str, Any] | None:
        table, calls = TABLES["call_resolutions"], TABLES["external_calls"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repository_id,
                    table.c.call_id == call_id,
                    table.c.run_id == run_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        call = connection.execute(sa.select(calls).where(calls.c.id == call_id)).mappings().one()
        if row["actor"] != actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "核对操作者不匹配")
        if resolver.provider != call["provider"] or resolver.signature != row["resolver_signature"]:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "核对 Adapter 签名已改变")
        if (
            call["status"] != "succeeded"
            or call["result_ref"] != row["result_ref"]
            or call["request_digest"] != row["request_digest"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对记录与当前调用结果不符")
        saved = parse_contract(
            self.history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
        )
        response = self.history.objects.get(saved.response_ref, ObjectType.BLOB)
        if not saved.complete or saved.error is not None or saved.response_bytes != len(response):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对结果或响应不完整")
        self.history.objects.get(row["evidence_ref"], ObjectType.BLOB)
        return dict(row)

    def _context(
        self,
        connection: Connection,
        run: RowMapping,
        call_id: UUID,
        actor: str,
        expected_version: int,
        resolver: OutcomeResolver,
    ) -> tuple[RowMapping, RowMapping | None]:
        branches, jobs, calls = TABLES["branches"], TABLES["jobs"], TABLES["external_calls"]
        branch = (
            connection.execute(
                sa.select(branches)
                .where(
                    branches.c.repository_id == run["repository_id"],
                    branches.c.name == run["branch_name"],
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        job = (
            connection.execute(sa.select(jobs).where(jobs.c.run_id == run["id"]).with_for_update())
            .mappings()
            .first()
        )
        if (
            run["status"] != "outcome_unknown"
            or run["state_version"] != expected_version
            or run["checkpoint_commit_id"] is None
            or branch["head_commit_id"] != run["checkpoint_commit_id"]
            or branch["active_writer_run_id"] != run["id"]
            or (job is not None and job["status"] != "outcome_unknown")
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "Run 尚未安全停止或核对版本已改变")
        allowed = [run["id"]]
        if job is not None and job["origin_run_id"] is not None:
            current = run["origin_run_id"]
            while current is not None:
                if current in allowed or len(allowed) >= 100:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对来源链非法")
                allowed.append(current)
                if current == job["origin_run_id"]:
                    break
                current = self.history._run(connection, run["repository_id"], current)[
                    "origin_run_id"
                ]
            if allowed[-1] != job["origin_run_id"]:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对来源链没有到达 Job 根")
        call = (
            connection.execute(
                sa.select(calls)
                .where(
                    calls.c.repository_id == run["repository_id"],
                    calls.c.id == call_id,
                    calls.c.run_id.in_(allowed),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if call is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "调用不属于当前 Run 的恢复路径")
        if resolver.provider != call["provider"] or resolver.signature != call["signature_digest"]:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "核对 Adapter 与原调用不符")
        if call["status"] not in ("started", "outcome_unknown") or call["result_ref"] is not None:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "调用不再处于未决状态")
        approvals = TABLES["approvals"]
        owner = connection.execute(
            sa.select(approvals.c.actor).where(
                approvals.c.repository_id == run["repository_id"],
                approvals.c.consumed_call_id == call_id,
            )
        ).scalar_one_or_none()
        if owner is None and job is not None:
            owner = connection.execute(
                sa.select(TABLES["idempotency_keys"].c.actor).where(
                    TABLES["idempotency_keys"].c.job_id == job["id"],
                    TABLES["idempotency_keys"].c.route == f"POST /tasks/{run['task_id']}/runs",
                )
            ).scalar_one()
        if owner is not None and owner != actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "核对操作者与原动作授权不符")
        return call, job
