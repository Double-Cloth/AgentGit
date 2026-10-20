"""调用意图/结果分别耐久保存；实际 Adapter 调用期间不持有数据库事务。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallOccurrence, EffectClass, parse_contract
from agentgit.core.research import CallTrace, SavedCallOutcome
from agentgit.core.snapshots import EventPayload
from agentgit.runtime.ports import CallOutcome
from agentgit.services.history import TABLES, EventInput, HistoryService


class RecordedCallGateway:
    def __init__(
        self,
        history: HistoryService,
        repository_id: UUID,
        run_id: UUID,
        checkpoint: Callable[[], tuple[str | None, int]],
    ):
        self.history, self.repository_id, self.run_id = history, repository_id, run_id
        self.checkpoint = checkpoint

    def call(
        self,
        *,
        step: str,
        attempt: int,
        provider: str,
        signature: str,
        effect: EffectClass,
        request: bytes,
        invoke: Callable[[], bytes],
        max_bytes: int,
    ) -> CallOutcome:
        history = self.history
        request_obj = history.objects.put(ObjectType.BLOB, request)
        occurrence = CallOccurrence(
            logical_step_id=step,
            call_index=0,
            attempt=attempt,
            provider=provider,
            signature_digest=signature,
            request_digest=request_obj.id,
        )
        head, version = self.checkpoint()
        intent = history.objects.put_json(
            ObjectType.CALL,
            {
                "schema_version": 1,
                "occurrence": occurrence.model_dump(mode="json"),
                "effect": effect.value,
                "request_ref": request_obj.id,
                "checkpoint": head,
                "max_response_bytes": max_bytes,
            },
        )
        calls = TABLES["external_calls"]
        call_id = uuid4()
        started_event = EventInput(
            "llm.requested" if provider.startswith("llm:") else "tool.started",
            step,
            {
                "occurrence": occurrence.model_dump(mode="json"),
                "request_ref": request_obj.id,
                "effect": effect.value,
            },
        )
        started_payload = history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=started_event.payload).model_dump(mode="json")
        )
        with history.engine.begin() as connection:
            run = history._run(connection, self.repository_id, self.run_id, lock=True)
            branch = TABLES["branches"]
            row = (
                connection.execute(
                    sa.select(branch)
                    .where(
                        branch.c.repository_id == self.repository_id,
                        branch.c.name == run["branch_name"],
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                run["status"] not in ("planning", "running")
                or run["checkpoint_commit_id"] != head
                or row["head_commit_id"] != head
                or row["version"] != version
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "调用前 Run/HEAD 已改变")
            existing = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == self.repository_id,
                        calls.c.run_id == self.run_id,
                        calls.c.logical_step_id == step,
                        calls.c.call_index == 0,
                        calls.c.attempt == attempt,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if (
                    existing["request_digest"] != request_obj.id
                    or existing["signature_digest"] != signature
                    or existing["provider"] != provider
                    or existing["effect"] != effect.value
                ):
                    raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "已录制调用的参数或签名不符")
                original = history.objects.get_json(existing["intent_ref"], ObjectType.CALL)
                if (
                    original.get("checkpoint") != head
                    or original.get("max_response_bytes") != max_bytes
                ):
                    raise AgentGitError(
                        ErrorCode.REPLAY_MISMATCH, "已录制调用的 checkpoint 或响应限制不符"
                    )
                if existing["result_ref"] is None:
                    raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "已有调用尚无耐久结果，禁止重发")
                saved = history.objects.get_json(existing["result_ref"], ObjectType.CALL)
                return self._outcome(occurrence, saved)
            for role, obj in (("request", request_obj), ("intent", intent)):
                history._ref(
                    connection, self.repository_id, obj, "external_call", str(call_id), role
                )
            connection.execute(
                calls.insert().values(
                    id=call_id,
                    repository_id=self.repository_id,
                    run_id=self.run_id,
                    logical_step_id=step,
                    call_index=0,
                    attempt=attempt,
                    provider=provider,
                    signature_digest=signature,
                    request_digest=request_obj.id,
                    effect=effect.value,
                    intent_ref=intent.id,
                    status="started",
                )
            )
            seq = int(run["last_event_seq"]) + 1
            history._event(
                connection, self.repository_id, self.run_id, seq, started_event, started_payload
            )
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.run_id)
                .values(last_event_seq=seq)
            )
            started_seq = seq
        # Adapter 可以执行磁盘操作；此处已退出事务。
        error: AgentGitError | None = None
        response = b""
        response_bytes = 0
        complete = True
        try:
            response = invoke()
            if not isinstance(response, bytes):
                response = b""
                raise AgentGitError(ErrorCode.VALIDATION, "Adapter 响应必须为 bytes")
            response_bytes = len(response)
            if len(response) > max_bytes:
                response = response[:max_bytes]
                complete = False
                error = AgentGitError(ErrorCode.OUTPUT_LIMIT, "调用响应超过上限，录制已截断")
        except AgentGitError as exc:
            error = exc
        except Exception:
            error = AgentGitError(
                ErrorCode.MODEL_ERROR if provider.startswith("llm:") else ErrorCode.TOOL_ERROR,
                "本地 Adapter 调用失败",
            )
        response_obj = history.objects.put(ObjectType.BLOB, response)
        saved = SavedCallOutcome(
            response_ref=response_obj.id,
            response_bytes=response_bytes,
            complete=complete,
            error=error.as_dict() if error else None,
        ).model_dump(mode="json")
        result_obj = history.objects.put_json(ObjectType.CALL, saved)
        completed_event = EventInput(
            "call.failed"
            if error
            else "llm.completed"
            if provider.startswith("llm:")
            else "tool.completed",
            step,
            {
                "occurrence": occurrence.model_dump(mode="json"),
                "result_ref": result_obj.id,
                "error": error.as_dict() if error else None,
            },
            started_seq,
        )
        completed_payload = history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=completed_event.payload).model_dump(mode="json")
        )
        with history.engine.begin() as connection:
            run = history._run(connection, self.repository_id, self.run_id, lock=True)
            for role, obj in (("response", response_obj), ("result", result_obj)):
                history._ref(
                    connection, self.repository_id, obj, "external_call", str(call_id), role
                )
            connection.execute(
                calls.update()
                .where(calls.c.id == call_id, calls.c.status == "started")
                .values(status="failed" if error else "succeeded", result_ref=result_obj.id)
            )
            seq = int(run["last_event_seq"]) + 1
            history._event(
                connection, self.repository_id, self.run_id, seq, completed_event, completed_payload
            )
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.run_id)
                .values(last_event_seq=seq)
            )
        return self._outcome(occurrence, saved)

    def _outcome(self, occurrence: CallOccurrence, saved: dict[str, Any]) -> CallOutcome:
        # 原始响应只从已核验 CAS 读取，缺失时明确失败，不重新调用 Adapter。
        validated = parse_contract(encode(saved), SavedCallOutcome)
        response_ref = validated.response_ref
        payload = self.history.objects.get(response_ref, ObjectType.BLOB)
        if validated.response_bytes < len(payload) or (
            validated.complete and validated.response_bytes != len(payload)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "调用响应长度与录制不符")
        error = validated.error
        failure = AgentGitError(ErrorCode(error["code"]), error["message"]) if error else None
        trace = CallTrace(
            logical_step_id=occurrence.logical_step_id,
            call_index=occurrence.call_index,
            attempt=occurrence.attempt,
            provider=occurrence.provider,
            signature_digest=occurrence.signature_digest,
            request_ref=occurrence.request_digest,
            response_ref=response_ref,
            error_code=failure.code.value if failure else None,
        )
        return CallOutcome(payload, trace, failure, validated.response_bytes)
