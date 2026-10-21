"""核验同一 Job 恢复链内的未知封存及原调用意图。"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import (
    CallIntent,
    Contract,
    EnvironmentManifest,
    Identifier,
    parse_contract,
)
from agentgit.core.research import AgentState
from agentgit.replay.unknown import RecordedUnknownStop
from agentgit.services.history import TABLES
from agentgit.services.recorded_settlements import SettlementReader

if TYPE_CHECKING:
    from agentgit.services.recordings import RecordingReader


class UnknownStopEvent(Contract):
    pending_calls: tuple[Identifier, ...]


class UnknownStopReader:
    def __init__(self, reader: "RecordingReader"):
        self.reader, self.history = reader, reader.history

    def load(
        self,
        repository: UUID,
        source: UUID,
        previous_commit: str,
        source_commit: str,
        data: dict[str, Any],
        environment: EnvironmentManifest,
        account: Callable[[bytes], bytes],
    ) -> RecordedUnknownStop:
        event = parse_contract(encode({"schema_version": 1, **data}), UnknownStopEvent)
        if len(event.pending_calls) != 1:
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "串行 Runtime 封存必须精确对应一个未决调用"
            )
        call_id = event.pending_calls[0]
        allowed = SettlementReader(self.reader)._allowed(repository, source)
        with self.history.engine.connect() as connection:
            calls = TABLES["external_calls"]
            row = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository, calls.c.id == UUID(call_id)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "未知封存缺少原调用索引")
        if (
            row["run_id"] not in allowed
            or row["origin_call_id"] is not None
            or row["status"] not in ("started", "outcome_unknown", "succeeded")
            or (row["status"] == "succeeded") != (row["result_ref"] is not None)
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知封存调用不属于自身恢复链或状态不符")
        intent = parse_contract(
            account(
                self.reader._owned(
                    repository,
                    row["intent_ref"],
                    ObjectType.CALL,
                    "external_call",
                    call_id,
                    "intent",
                )
            ),
            CallIntent,
        )
        occurrence = intent.occurrence
        if (
            intent.checkpoint is None
            or intent.checkpoint not in self.reader.versions.ancestors(repository, previous_commit)
            or intent.effect.value != row["effect"]
            or intent.request_ref != row["request_digest"]
            or any(
                getattr(occurrence, key) != row[key]
                for key in (
                    "logical_step_id",
                    "call_index",
                    "attempt",
                    "provider",
                    "signature_digest",
                    "request_digest",
                )
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知封存调用与原意图或检查点不符")
        declared = (
            environment.model_signature.rsplit(":", 1)[-1] == occurrence.signature_digest
            if occurrence.provider.startswith("llm:")
            else occurrence.signature_digest in environment.tool_signatures
        )
        if not declared:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "未知封存签名未在冻结环境中声明")
        if row["status"] == "succeeded":
            # 核对发生在封存之后；只验证来源，绝不提前读取或消费确认响应。
            with self.history.engine.connect() as connection:
                resolutions = TABLES["call_resolutions"]
                proof = connection.execute(
                    sa.select(resolutions.c.id).where(
                        resolutions.c.repository_id == repository,
                        resolutions.c.call_id == row["id"],
                        resolutions.c.run_id.in_(allowed),
                        resolutions.c.result_ref == row["result_ref"],
                        resolutions.c.request_digest == row["request_digest"],
                        resolutions.c.resolver_signature == row["signature_digest"],
                    )
                ).first()
            if proof is None:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "已确认调用缺少独立核对来源")
        request = account(
            self.reader._owned(
                repository, intent.request_ref, ObjectType.BLOB, "external_call", call_id, "request"
            )
        )
        snapshot = self.history.restore(repository, intent.checkpoint)
        account(encode(snapshot.model_dump(mode="json")))
        return RecordedUnknownStop(
            source_commit,
            call_id,
            intent,
            request,
            parse_contract(encode(snapshot.data), AgentState),
        )
