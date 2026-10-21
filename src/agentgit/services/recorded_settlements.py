"""读取已保存的可信核对证明，不调用 Resolver 或重发原动作。"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import (
    CallIntent,
    Contract,
    Digest,
    EnvironmentManifest,
    Identifier,
    RunStatus,
    parse_contract,
)
from agentgit.core.research import AgentState, SavedCallOutcome
from agentgit.replay.recorded import Recording
from agentgit.replay.settlements import RecordedSettlement
from agentgit.services.history import TABLES

if TYPE_CHECKING:
    from agentgit.services.recordings import RecordingReader


class ResolutionEvent(Contract):
    resolution_id: Identifier
    origin_call_id: Identifier
    actor: str
    result_ref: Digest
    evidence_ref: Digest


class SettlementReader:
    def __init__(self, reader: "RecordingReader"):
        self.reader, self.history = reader, reader.history

    def _allowed(self, repository: UUID, source: UUID) -> set[UUID]:
        with self.history.engine.connect() as connection:
            lineage = self.reader.versions._family(connection, repository, source)
            jobs = TABLES["jobs"]
            candidates = list(
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.repository_id == repository,
                        sa.or_(jobs.c.run_id.in_(lineage), jobs.c.origin_run_id.in_(lineage)),
                    )
                ).mappings()
            )
        if not candidates:
            return {source}
        positions = {run: index for index, run in enumerate(lineage)}

        def distance(row: Any) -> int:
            return min(positions.get(row["run_id"], 100), positions.get(row["origin_run_id"], 100))

        candidates.sort(key=distance)
        if len(candidates) > 1 and distance(candidates[0]) == distance(candidates[1]):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对来源没有唯一 Job 恢复根")
        job = candidates[0]
        root = job["origin_run_id"] or job["run_id"]
        if root not in positions:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对来源未到达自身 Job 根")
        return set(lineage[: positions[root] + 1])

    def load(
        self,
        repository: UUID,
        source: UUID,
        at_commit: str,
        source_commit: str,
        data: dict[str, Any],
        environment: EnvironmentManifest,
        account: Callable[[bytes], bytes],
    ) -> RecordedSettlement:
        event = parse_contract(encode({"schema_version": 1, **data}), ResolutionEvent)
        allowed = self._allowed(repository, source)
        with self.history.engine.connect() as connection:
            resolutions, calls = TABLES["call_resolutions"], TABLES["external_calls"]
            proof = (
                connection.execute(
                    sa.select(resolutions).where(
                        resolutions.c.repository_id == repository,
                        resolutions.c.id == UUID(event.resolution_id),
                    )
                )
                .mappings()
                .first()
            )
            call = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository,
                        calls.c.id == UUID(event.origin_call_id),
                    )
                )
                .mappings()
                .first()
            )
        if proof is None or call is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "可信核对缺少来源索引")
        if (
            proof["run_id"] != source
            or call["run_id"] not in allowed
            or proof["call_id"] != call["id"]
            or proof["checkpoint_commit_id"] != at_commit
            or proof["actor"] != event.actor
            or proof["result_ref"] != event.result_ref
            or proof["evidence_ref"] != event.evidence_ref
            or proof["request_digest"] != call["request_digest"]
            or proof["resolver_signature"] != call["signature_digest"]
            or call["status"] != "succeeded"
            or call["result_ref"] != proof["result_ref"]
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "可信核对事件、调用与证明绑定不符")
        call_id, resolution_id = str(call["id"]), str(proof["id"])
        intent = parse_contract(
            account(
                self.reader._owned(
                    repository,
                    call["intent_ref"],
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
            or intent.request_ref != call["request_digest"]
            or intent.effect.value != call["effect"]
            or any(
                getattr(occurrence, key) != call[key]
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
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "可信核对与原调用意图不符")
        declared = (
            environment.model_signature.rsplit(":", 1)[-1] == occurrence.signature_digest
            if occurrence.provider.startswith("llm:")
            else occurrence.signature_digest in environment.tool_signatures
        )
        if not declared:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "核对结果的签名未在环境中声明")
        evidence = account(
            self.reader._owned(
                repository,
                proof["evidence_ref"],
                ObjectType.BLOB,
                "call_resolution",
                resolution_id,
                "evidence",
            )
        )
        if not evidence:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "可信核对缺少肯定回执证据")
        saved = parse_contract(
            account(
                self.reader._owned(
                    repository,
                    proof["result_ref"],
                    ObjectType.CALL,
                    "call_resolution",
                    resolution_id,
                    "result",
                )
            ),
            SavedCallOutcome,
        )
        if not saved.complete or saved.error is not None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "可信核对结果不是完整成功回执")
        request = account(
            self.reader._owned(
                repository, intent.request_ref, ObjectType.BLOB, "external_call", call_id, "request"
            )
        )
        response = account(
            self.reader._owned(
                repository,
                saved.response_ref,
                ObjectType.BLOB,
                "call_resolution",
                resolution_id,
                "response",
            )
        )
        record = Recording(intent, saved, request, response, call_id)
        record.outcome()
        if intent.checkpoint not in self.reader.versions.ancestors(repository, at_commit):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "核对结果的调用前检查点不在来源路径")
        snapshot = self.history.restore(repository, intent.checkpoint)
        account(encode(snapshot.model_dump(mode="json")))
        before = parse_contract(encode(snapshot.data), AgentState)
        unknown = parse_contract(
            encode(self.history.restore(repository, at_commit).data), AgentState
        )
        if unknown.status != RunStatus.OUTCOME_UNKNOWN:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "可信核对没有对应的未知状态检查点")
        return RecordedSettlement(at_commit, source_commit, before, record)
