"""调用意图/结果分别耐久保存；实际 Adapter 调用期间不持有数据库事务。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import SANDBOX_PROVIDERS
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass, parse_contract
from agentgit.core.research import CallTrace, SavedCallOutcome
from agentgit.core.snapshots import EventPayload
from agentgit.runtime.ports import CallOutcome
from agentgit.services.approvals import ApprovalService
from agentgit.services.history import TABLES, EventInput, HistoryService
from agentgit.services.managed_calls import ManagedCallContext, ManagedCalls
from agentgit.storage.objects import StoredObject


class RecordedCallGateway:
    def __init__(
        self,
        history: HistoryService,
        repository_id: UUID,
        run_id: UUID,
        checkpoint: Callable[[], tuple[str | None, int]],
        *,
        origin_runs: tuple[UUID, ...] = (),
        materialize: Callable[[str, bytes, bytes], None] | None = None,
        after_result: Callable[[], None] | None = None,
        authorization: ApprovalService | None = None,
        managed: ManagedCalls | None = None,
    ):
        self.history, self.repository_id, self.run_id = history, repository_id, run_id
        self.checkpoint = checkpoint
        self.authorization = authorization
        self.managed = managed
        self.origin_runs, self.materialize, self.after_result = (
            origin_runs,
            materialize,
            after_result,
        )

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
        if type(max_bytes) is not int or not 1 <= max_bytes <= 8 * 1024 * 1024:
            raise AgentGitError(ErrorCode.VALIDATION, "响应上限必须为 1–8388608 字节")
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
        if self.authorization is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "调用缺少本地工具策略")
        self.authorization.policy.rule(occurrence, effect)
        if self.origin_runs:
            recovered = self._reuse_origin(occurrence, effect, max_bytes)
            if recovered is not None:
                return recovered
        head, version = self.checkpoint()
        intent = history.objects.put_json(
            ObjectType.CALL,
            CallIntent(
                occurrence=occurrence,
                effect=effect,
                request_ref=request_obj.id,
                checkpoint=head,
                max_response_bytes=max_bytes,
            ).model_dump(mode="json"),
        )
        calls = TABLES["external_calls"]
        call_id = uuid4()
        context = ManagedCallContext(
            self.repository_id,
            self.run_id,
            call_id,
            occurrence,
            head,
            request,
            intent.id,
            max_bytes,
        )
        if provider in SANDBOX_PROVIDERS and self.managed is not None:
            self.managed.stage(context)
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
            history.assert_writer(connection, self.repository_id, self.run_id)
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
                run["status"] not in ("planning", "running", "waiting_approval")
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
                return self._cached_outcome(occurrence, saved)
            if run["control_status"] == "requested" and run["control_request"] in (
                "pause",
                "cancel",
            ):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "调用意图保存前收到控制请求")
            if provider in SANDBOX_PROVIDERS and self.managed is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "容器工具缺少受租约保护的执行服务")
            admission_error = self.authorization.admit(
                connection, run, occurrence, effect, max_bytes, call_id
            )
            started_seq = 0
            if admission_error is None:
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
                if provider in SANDBOX_PROVIDERS:
                    assert self.managed is not None
                    self.managed.prepare(connection, context)
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
        if admission_error is not None:
            raise admission_error
        # Adapter 可以执行磁盘操作；此处已退出事务。
        error: AgentGitError | None = None
        response = b""
        response_bytes = 0
        complete = True
        try:
            if provider in SANDBOX_PROVIDERS:
                assert self.managed is not None
                managed_result = self.managed.invoke(context)
                response, error = managed_result.payload, managed_result.error
            else:
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
            error = (
                AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "写动作未取得可确认的结果，需要核对")
                if effect in (EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.IRREVERSIBLE)
                and exc.code not in (ErrorCode.OUTCOME_UNKNOWN, ErrorCode.OUTPUT_LIMIT)
                else exc
            )
        except Exception:
            error = AgentGitError(
                ErrorCode.OUTCOME_UNKNOWN
                if effect in (EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.IRREVERSIBLE)
                else ErrorCode.MODEL_ERROR
                if provider.startswith("llm:")
                else ErrorCode.TOOL_ERROR,
                "Adapter 调用中断，外部结果需要核对",
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
            "call.outcome_unknown"
            if error and error.code == ErrorCode.OUTCOME_UNKNOWN
            else "call.failed"
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
            history.assert_writer(connection, self.repository_id, self.run_id)
            unknown = error is not None and error.code == ErrorCode.OUTCOME_UNKNOWN
            for role, obj in (
                ("response", response_obj),
                ("uncertain_diagnostic" if unknown else "result", result_obj),
            ):
                history._ref(
                    connection, self.repository_id, obj, "external_call", str(call_id), role
                )
            if provider in SANDBOX_PROVIDERS:
                assert self.managed is not None
                self.managed.record(connection, context, response_obj, result_obj)
            connection.execute(
                calls.update()
                .where(calls.c.id == call_id, calls.c.status == "started")
                .values(
                    status="outcome_unknown" if unknown else "failed" if error else "succeeded",
                    result_ref=None if unknown else result_obj.id,
                )
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
        if self.after_result is not None:
            self.after_result()
        return self._outcome(occurrence, saved)

    def _cached_outcome(self, occurrence: CallOccurrence, saved: dict[str, Any]) -> CallOutcome:
        outcome = self._outcome(occurrence, saved)
        if outcome.error is None and self.materialize is not None:
            self.materialize(
                occurrence.provider,
                self.history.objects.get(occurrence.request_digest, ObjectType.BLOB),
                outcome.payload,
            )
        return outcome

    def _reuse_origin(
        self, occurrence: CallOccurrence, effect: EffectClass, max_bytes: int
    ) -> CallOutcome | None:
        history, calls = self.history, TABLES["external_calls"]
        head, _ = self.checkpoint()
        saved: dict[str, Any] | None = None
        with history.engine.begin() as connection:
            run = history._run(connection, self.repository_id, self.run_id, lock=True)
            history.assert_writer(connection, self.repository_id, self.run_id)
            key = (
                calls.c.repository_id == self.repository_id,
                calls.c.logical_step_id == occurrence.logical_step_id,
                calls.c.call_index == occurrence.call_index,
                calls.c.attempt == occurrence.attempt,
            )
            if (
                connection.execute(
                    sa.select(calls.c.id).where(*key, calls.c.run_id == self.run_id)
                ).first()
                is not None
            ):
                return None
            origin = None
            for run_id in self.origin_runs:
                origin = (
                    connection.execute(sa.select(calls).where(*key, calls.c.run_id == run_id))
                    .mappings()
                    .first()
                )
                if origin is not None:
                    break
            if origin is None:
                return None
            if (
                origin["request_digest"] != occurrence.request_digest
                or origin["signature_digest"] != occurrence.signature_digest
                or origin["provider"] != occurrence.provider
                or origin["effect"] != effect.value
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "恢复调用与来源录制不符")
            intent = history.objects.get_json(origin["intent_ref"], ObjectType.CALL)
            if intent.get("checkpoint") != head or intent.get("max_response_bytes") != max_bytes:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "恢复调用的 checkpoint 或限制不符")
            if origin["result_ref"] is None:
                raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "来源调用没有结果，不能在恢复中重发")
            saved = history.objects.get_json(origin["result_ref"], ObjectType.CALL)
            # 读取并校验完整结果后才建立当前 Run 的复用记录。
            self._outcome(occurrence, saved)
            call_id = uuid4()
            for role, object_id in (
                ("request", origin["request_digest"]),
                ("intent", origin["intent_ref"]),
                ("result", origin["result_ref"]),
                ("response", saved["response_ref"]),
            ):
                row = (
                    connection.execute(
                        sa.select(TABLES["objects"]).where(TABLES["objects"].c.id == object_id)
                    )
                    .mappings()
                    .one()
                )
                item = StoredObject(
                    row["id"],
                    ObjectType(row["object_type"]),
                    row["media_type"],
                    row["byte_length"],
                    row["storage_key"],
                    row["codec"],
                )
                history._ref(
                    connection, self.repository_id, item, "external_call", str(call_id), role
                )
            connection.execute(
                calls.insert().values(
                    id=call_id,
                    repository_id=self.repository_id,
                    run_id=self.run_id,
                    logical_step_id=occurrence.logical_step_id,
                    call_index=occurrence.call_index,
                    attempt=occurrence.attempt,
                    provider=occurrence.provider,
                    signature_digest=occurrence.signature_digest,
                    request_digest=occurrence.request_digest,
                    effect=effect.value,
                    intent_ref=origin["intent_ref"],
                    result_ref=origin["result_ref"],
                    origin_call_id=origin["id"],
                    status=origin["status"],
                )
            )
            event = EventInput(
                "call.reused",
                occurrence.logical_step_id,
                {
                    "occurrence": occurrence.model_dump(mode="json"),
                    "origin_run_id": str(origin["run_id"]),
                    "origin_call_id": str(origin["id"]),
                    "result_ref": origin["result_ref"],
                },
            )
            payload = history.objects.put_json(
                ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
            )
            seq = int(run["last_event_seq"]) + 1
            history._event(connection, self.repository_id, self.run_id, seq, event, payload)
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == self.run_id)
                .values(last_event_seq=seq)
            )
        return self._cached_outcome(occurrence, saved)

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
