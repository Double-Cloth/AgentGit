"""只从已授权来源路径读取录制；不构造任何真实 Adapter 或执行服务。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import (
    SANDBOX_PROVIDERS,
    CodeArguments,
    ExecutionManifest,
    ExecutionPolicy,
    ExecutionResult,
    TerminalArguments,
    execution_signature,
)
from agentgit.core.models import (
    CallIntent,
    CallOccurrence,
    CommitManifest,
    EnvironmentManifest,
    parse_contract,
)
from agentgit.core.research import AgentState, SavedCallOutcome
from agentgit.core.snapshots import EventManifest, EventPayload, LogicalEvent, StateSnapshot
from agentgit.core.versions import RecordedRange
from agentgit.replay.recorded import Recording, UncertainRecording
from agentgit.replay.recovery import CleanupProof, DiscardEvent, RecordedDiscard
from agentgit.replay.settlements import RecordedSettlement
from agentgit.replay.unknown import RecordedUnknownStop
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.versions import HistorySelection, VersionService

MAX_RECORDED_BYTES = 64 * 1024 * 1024
MAX_RECORDED_EVENTS = 50000


@dataclass(frozen=True)
class RecordedProjection:
    origin: "RecordedBundle"
    commits: tuple[str, ...]
    events: tuple[tuple[str, dict[str, Any]] | None, ...]


@dataclass(frozen=True)
class RecordedBundle:
    selection: HistorySelection
    environment: EnvironmentManifest
    checkpoints: tuple[tuple[str, StateSnapshot], ...]
    recordings: tuple[Recording, ...]
    discards: tuple[RecordedDiscard, ...] = ()
    projection: RecordedProjection | None = None
    settlements: tuple[RecordedSettlement, ...] = ()
    unknown_stops: tuple[RecordedUnknownStop, ...] = ()


class RecordingReader:
    def __init__(self, history: HistoryService):
        self.history, self.versions = history, VersionService(history)

    def _owned(
        self,
        repository: UUID,
        object_id: str,
        kind: ObjectType,
        owner_type: str,
        owner_id: str,
        role: str,
    ) -> bytes:
        refs = TABLES["object_refs"]
        with self.history.engine.connect() as connection:
            exists = connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository,
                    refs.c.object_id == object_id,
                    refs.c.owner_type == owner_type,
                    refs.c.owner_id == owner_id,
                    refs.c.role == role,
                )
            ).first()
        if exists is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "录制对象缺少来源引用归属")
        return self.history.objects.get(object_id, kind)

    def _physical(self, repository: UUID, row: dict[str, Any], family: set[UUID]) -> dict[str, Any]:
        current = row
        seen: set[UUID] = set()
        while current["origin_call_id"] is not None:
            identity = current["origin_call_id"]
            if identity in seen or len(seen) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "录制来源调用链包含循环或过长")
            seen.add(identity)
            with self.history.engine.connect() as connection:
                origin = (
                    connection.execute(
                        sa.select(TABLES["external_calls"]).where(
                            TABLES["external_calls"].c.repository_id == repository,
                            TABLES["external_calls"].c.id == identity,
                        )
                    )
                    .mappings()
                    .first()
                )
            if origin is None or origin["run_id"] not in family:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "调用 provenance 不属于来源 Run 链")
            if any(
                origin[key] != row[key]
                for key in (
                    "logical_step_id",
                    "call_index",
                    "attempt",
                    "provider",
                    "signature_digest",
                    "request_digest",
                    "effect",
                    "intent_ref",
                    "result_ref",
                    "status",
                )
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "复用调用与原录制身份不符")
            current = dict(origin)
        return current

    def _discard(
        self,
        repository: UUID,
        source: UUID,
        family: set[UUID],
        commit_id: str,
        data: dict[str, Any],
        environment: EnvironmentManifest,
    ) -> RecordedDiscard:
        if type(data.get("charged_tool_calls")) is not int:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "丢弃预算必须为精确整数")
        event = parse_contract(encode({"schema_version": 1, **data}), DiscardEvent)
        calls, executions, refs = (
            TABLES["external_calls"],
            TABLES["sandbox_executions"],
            TABLES["object_refs"],
        )
        with self.history.engine.connect() as connection:
            call = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository, calls.c.id == UUID(event.call_id)
                    )
                )
                .mappings()
                .first()
            )
            execution = (
                connection.execute(
                    sa.select(executions).where(
                        executions.c.repository_id == repository,
                        executions.c.call_id == UUID(event.call_id),
                    )
                )
                .mappings()
                .first()
            )
            proofs = list(
                connection.execute(
                    sa.select(refs.c.object_id).where(
                        refs.c.repository_id == repository,
                        refs.c.owner_type == "sandbox_execution",
                        refs.c.owner_id == event.call_id,
                        refs.c.role == "recovery_cleanup",
                    )
                ).scalars()
            )
        if (
            call is None
            or execution is None
            or call["run_id"] not in family
            or execution["run_id"] != call["run_id"]
            or call["result_ref"] is not None
            or execution["result_ref"] is not None
            or call["status"] not in ("started", "outcome_unknown")
            or call["effect"] != "NON_IDEMPOTENT_WRITE"
            or execution["status"] != "outcome_unknown"
            or not execution["cleanup_confirmed"]
            or proofs != [event.cleanup_evidence_ref]
        ):
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "录制丢弃缺少原执行或可信清理事实")
        raw = self._owned(
            repository,
            event.cleanup_evidence_ref,
            ObjectType.BLOB,
            "execution_discard",
            str(source),
            event.stopped_execution.occurrence.request_digest,
        )
        if len(raw) > 4096:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "清理证明超过固定上限")
        proof = parse_contract(raw, CleanupProof)
        if (
            not proof.cleanup_confirmed
            or proof.container_name != execution["container_name"]
            or proof.container_id != execution["container_id"]
            or proof.previous_fencing_token != execution["fencing_token"]
            or proof.observer_fencing_token <= proof.previous_fencing_token
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "丢弃清理证明与原容器身份不符")
        call_id = event.call_id
        intent = parse_contract(
            self._owned(
                repository, call["intent_ref"], ObjectType.CALL, "external_call", call_id, "intent"
            ),
            CallIntent,
        )
        occurrence = event.stopped_execution.occurrence
        policy = parse_contract(
            self._owned(
                repository,
                execution["policy_ref"],
                ObjectType.BLOB,
                "sandbox_execution",
                call_id,
                "policy",
            ),
            ExecutionPolicy,
        )
        if (
            intent.occurrence != occurrence
            or intent.effect.value != call["effect"]
            or occurrence.provider not in SANDBOX_PROVIDERS
            or intent.checkpoint != execution["checkpoint_commit_id"]
            or intent.request_ref != execution["request_digest"]
            or intent.request_ref != occurrence.request_digest
            or execution["attempt"] != occurrence.attempt
            or event.stopped_execution.input_manifest_ref != execution["input_manifest_ref"]
            or event.stopped_execution.execution_policy_ref != execution["policy_ref"]
            or policy.digest() != execution["policy_ref"]
            or policy.image_id != execution["image_id"]
            or execution_signature(occurrence.provider, policy) != occurrence.signature_digest
            or occurrence.signature_digest not in environment.tool_signatures
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
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "丢弃证书、意图、执行索引与环境不符")
        request = self._owned(
            repository, intent.request_ref, ObjectType.BLOB, "external_call", call_id, "request"
        )
        arguments = (
            parse_contract(request, TerminalArguments)
            if occurrence.provider == "tool:terminal_exec/v1"
            else parse_contract(request, CodeArguments)
        )
        checkpoint = intent.checkpoint
        if checkpoint is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "丢弃录制没有原输入检查点")
        if (
            arguments.input_manifest_ref != execution["input_manifest_ref"]
            or arguments.execution_policy_ref != policy.digest()
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "丢弃请求与冻结输入或策略不符")
        manifest = parse_contract(
            self._owned(
                repository,
                arguments.input_manifest_ref,
                ObjectType.ARTIFACT_MANIFEST,
                "sandbox_input",
                str(call["run_id"]),
                checkpoint,
            ),
            ExecutionManifest,
        )
        for item in manifest.files:
            if (
                len(
                    self._owned(
                        repository,
                        item.object_ref,
                        ObjectType.BLOB,
                        "sandbox_input",
                        str(call["run_id"]),
                        checkpoint,
                    )
                )
                != item.byte_length
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "丢弃输入文件长度与原清单不符")
        extra = (
            arguments.command_ref
            if isinstance(arguments, TerminalArguments)
            else arguments.code_ref
        )
        for reference in (arguments.stdin_ref, extra):
            if reference is not None:
                self._owned(
                    repository,
                    reference,
                    ObjectType.BLOB,
                    "sandbox_input",
                    str(call["run_id"]),
                    checkpoint,
                )
        before = parse_contract(
            encode(self.history.restore(repository, checkpoint).data), AgentState
        )
        return RecordedDiscard(commit_id, event, intent, request, before)

    def _execution(self, repository: UUID, row: dict[str, Any], record: Recording) -> None:
        call_id = str(row["id"])
        provider = record.intent.occurrence.provider
        schema = TerminalArguments if provider == "tool:terminal_exec/v1" else CodeArguments
        request = parse_contract(record.request, schema)
        result = parse_contract(record.response, ExecutionResult)
        policy = parse_contract(
            self._owned(
                repository,
                result.execution_policy_ref,
                ObjectType.BLOB,
                "sandbox_execution",
                call_id,
                "policy",
            ),
            ExecutionPolicy,
        )
        if (
            result.input_manifest_ref != request.input_manifest_ref
            or result.execution_policy_ref != request.execution_policy_ref
            or result.execution_policy_ref != policy.digest()
            or result.image_id != policy.image_id
            or execution_signature(provider, policy) != record.intent.occurrence.signature_digest
            or result.command_ref
            != (request.command_ref if isinstance(request, TerminalArguments) else None)
            or result.code_ref != (request.code_ref if isinstance(request, CodeArguments) else None)
            or result.script_path
            != (request.script_path if isinstance(request, CodeArguments) else None)
            or result.interpreter
            != (policy.shell if isinstance(request, TerminalArguments) else policy.interpreter)
            or result.interpreter_version
            != (
                policy.shell_version
                if isinstance(request, TerminalArguments)
                else policy.interpreter_version
            )
            or result.error_code != (record.saved.error["code"] if record.saved.error else None)
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "执行录制与请求、签名或固定策略不符")
        if record.intent.checkpoint is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行录制缺少输入检查点")
        checkpoint = record.intent.checkpoint
        if isinstance(request, CodeArguments) and request.language != "python":
            raise AgentGitError(ErrorCode.UNSUPPORTED_LANGUAGE, "录制语言不在当前支持范围")

        def input_blob(object_id: str) -> bytes:
            return self._owned(
                repository,
                object_id,
                ObjectType.BLOB,
                "sandbox_input",
                str(row["run_id"]),
                checkpoint,
            )

        inputs = parse_contract(
            self._owned(
                repository,
                result.input_manifest_ref,
                ObjectType.ARTIFACT_MANIFEST,
                "sandbox_input",
                str(row["run_id"]),
                record.intent.checkpoint,
            ),
            ExecutionManifest,
        )
        for item in inputs.files:
            if len(input_blob(item.object_ref)) != item.byte_length:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行输入文件长度不符")
        for reference in (request.stdin_ref, result.command_ref, result.code_ref):
            if reference is not None:
                input_blob(reference)
        for reference in (result.stdout_ref, result.stderr_ref):
            payload = self._owned(
                repository, reference, ObjectType.BLOB, "sandbox_execution", call_id, "evidence"
            )
            if len(payload) > policy.max_log_bytes:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行日志超过冻结上限")
        if result.output_manifest_ref is not None:
            outputs = parse_contract(
                self._owned(
                    repository,
                    result.output_manifest_ref,
                    ObjectType.ARTIFACT_MANIFEST,
                    "sandbox_execution",
                    call_id,
                    "evidence",
                ),
                ExecutionManifest,
            )
            for item in outputs.files:
                payload = self._owned(
                    repository,
                    item.object_ref,
                    ObjectType.BLOB,
                    "sandbox_execution",
                    call_id,
                    "evidence",
                )
                if len(payload) != item.byte_length:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行输出文件长度不符")

    def load(
        self,
        repository: UUID,
        request: RecordedRange,
        *,
        expected_environment: EnvironmentManifest,
    ) -> RecordedBundle:
        # 来源 Run 与路径归属在缺对象映射之前验证，跨项目仍统一 not_found。
        with self.history.engine.connect() as connection:
            self.history._run(connection, repository, UUID(request.source_run_id))
        size = 0

        def account(payload: bytes) -> bytes:
            nonlocal size
            size += len(payload)
            if size > MAX_RECORDED_BYTES:
                raise AgentGitError(
                    ErrorCode.OUTPUT_LIMIT, "录制累计内容超过内存上限，请缩小起止范围"
                )
            return payload

        try:
            return self._read(repository, request, expected_environment, (), account)
        except AgentGitError as error:
            if error.code == ErrorCode.NOT_FOUND:
                raise AgentGitError(
                    ErrorCode.RECORDING_MISSING, "选定范围缺少录制或产物对象"
                ) from None
            raise

    def _read(
        self,
        repository: UUID,
        request: RecordedRange,
        environment: EnvironmentManifest,
        seen: tuple[UUID, ...],
        account: Callable[[bytes], bytes],
    ) -> RecordedBundle:
        source = UUID(request.source_run_id)
        if source in seen:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 来源证明链包含循环")
        if len(seen) >= 32:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Recorded 来源证明链超过 32 层")
        selection = self.versions.select_range(repository, request)
        with self.history.engine.connect() as connection:
            run = self.history._run(connection, repository, source)
        if run["mode"] == "recorded":
            from agentgit.services.replay_projections import ProjectionReader

            return ProjectionReader(self).load(
                repository, selection, environment, (*seen, source), account
            )
        return self._load(repository, selection, environment, account)

    def _load(
        self,
        repository: UUID,
        selection: HistorySelection,
        environment: EnvironmentManifest,
        account: Callable[[bytes], bytes],
    ) -> RecordedBundle:
        checkpoints: list[tuple[str, StateSnapshot]] = []
        with self.history.engine.connect() as connection:
            source = self.history._run(connection, repository, selection.source_run_id)
        raw_input = self.history.objects.get(environment.input_ref, ObjectType.BLOB)
        try:
            frozen_input = decode(raw_input)
        except AgentGitError as error:
            # 通用录制允许不透明输入；Hybrid 来源必须保持受控 JSON 校验。
            if source["mode"] == "hybrid" or error.code != ErrorCode.VALIDATION:
                raise
            frozen_input = None
        reference = (
            frozen_input.get("hybrid_policy_ref") if isinstance(frozen_input, dict) else None
        )
        if reference is not None:
            from agentgit.services.hybrid import policy_for_run

            if source["mode"] != "hybrid":
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "非 Hybrid 来源冒用了 policy 环境")
            expected_reference, policy = policy_for_run(
                self.history, repository, selection.source_run_id
            )
            if reference != expected_reference or policy.corpus_digest != frozen_input.get(
                "corpus_digest"
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Hybrid 环境与来源 Job 冻结输入不同")
        elif source["mode"] == "hybrid" and not (
            len(selection.commits) == 1 and selection.commits[0] == source["base_commit_id"]
        ):
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "Hybrid 来源缺少 policy 环境")
        expected_events: list[LogicalEvent] = []
        checkpoint_for_event: dict[int, str] = {}
        boundary_commits: dict[int, str] = {}
        checkpoint_after_event: dict[int, str] = {}
        for index, commit_id in enumerate(selection.commits):
            view = self.versions.checkout(repository, commit_id)
            account(encode(view))
            found = parse_contract(encode(view["environment"]), EnvironmentManifest)
            if found != environment:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "录制范围的冻结环境不兼容")
            checkpoints.append((commit_id, parse_contract(encode(view["snapshot"]), StateSnapshot)))
            if index:
                manifest = parse_contract(encode(view["manifest"]), CommitManifest)
                events = parse_contract(
                    account(
                        self.history.objects.get(
                            manifest.event_manifest_ref, ObjectType.EVENT_MANIFEST
                        )
                    ),
                    EventManifest,
                )
                if len(expected_events) + len(events.events) > MAX_RECORDED_EVENTS:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "录制事件超过有界上限")
                expected_events.extend(events.events)
                checkpoint_for_event.update(
                    {event.seq: selection.commits[index - 1] for event in events.events}
                )
                checkpoint_after_event.update({event.seq: commit_id for event in events.events})
                boundary_commits[manifest.event_range.last_seq] = commit_id
        with self.history.engine.connect() as connection:
            events_table, calls = TABLES["events"], TABLES["external_calls"]
            event_rows = list(
                connection.execute(
                    sa.select(events_table)
                    .where(
                        events_table.c.repository_id == repository,
                        events_table.c.run_id == selection.source_run_id,
                        events_table.c.seq > selection.from_seq,
                        events_table.c.seq <= selection.until_seq,
                    )
                    .order_by(events_table.c.seq)
                    .limit(MAX_RECORDED_EVENTS + 1)
                ).mappings()
            )
            call_rows = list(
                connection.execute(
                    sa.select(calls)
                    .where(
                        calls.c.repository_id == repository,
                        calls.c.run_id == selection.source_run_id,
                    )
                    .limit(10001)
                ).mappings()
            )
            family = set(self.versions._family(connection, repository, selection.source_run_id))
        if len(event_rows) > MAX_RECORDED_EVENTS or len(call_rows) > 10000:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "录制事件或调用数超过有界上限")
        indexed_events = [
            LogicalEvent(**self.history._logical_event(row, selection.source_run_id))
            for row in event_rows
        ]
        if (
            indexed_events != expected_events
            or len(indexed_events) != selection.until_seq - selection.from_seq
            or any(
                item.seq != seq for seq, item in enumerate(indexed_events, selection.from_seq + 1)
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源事件索引与检查点清单不一致")
        by_key = {
            (row["logical_step_id"], row["call_index"], row["attempt"]): dict(row)
            for row in call_rows
        }
        if len(by_key) != len(call_rows):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源调用索引包含重复 occurrence")
        completed: dict[tuple[str, int, int], tuple[int, dict[str, Any], str]] = {}
        discards: list[RecordedDiscard] = []
        settlements: list[RecordedSettlement] = []
        unknown_stops: list[RecordedUnknownStop] = []
        for event in indexed_events:
            if event.type == "run.outcome_unknown":
                from agentgit.services.recorded_unknowns import UnknownStopReader

                if event.seq not in boundary_commits:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "未知封存必须对应独立状态检查点")
                data = parse_contract(
                    account(self.history.objects.get(event.payload_ref, ObjectType.EVENT)),
                    EventPayload,
                ).data
                unknown_stops.append(
                    UnknownStopReader(self).load(
                        repository,
                        selection.source_run_id,
                        checkpoint_for_event[event.seq],
                        boundary_commits[event.seq],
                        data,
                        environment,
                        account,
                    )
                )
                continue
            if event.type == "sandbox.execution_discarded":
                if event.seq not in boundary_commits:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行丢弃必须对应独立状态检查点")
                data = parse_contract(
                    account(self.history.objects.get(event.payload_ref, ObjectType.EVENT)),
                    EventPayload,
                ).data
                discard = self._discard(
                    repository,
                    selection.source_run_id,
                    family,
                    boundary_commits[event.seq],
                    data,
                    environment,
                )
                account(encode(discard.before.model_dump(mode="json")))
                account(discard.request)
                discards.append(discard)
                continue
            if event.type == "call.reconciled":
                from agentgit.services.recorded_settlements import SettlementReader

                data = parse_contract(
                    account(self.history.objects.get(event.payload_ref, ObjectType.EVENT)),
                    EventPayload,
                ).data
                settlements.append(
                    SettlementReader(self).load(
                        repository,
                        selection.source_run_id,
                        checkpoint_for_event[event.seq],
                        checkpoint_after_event[event.seq],
                        data,
                        environment,
                        account,
                    )
                )
                continue
            if event.type not in (
                "tool.completed",
                "llm.completed",
                "call.failed",
                "call.outcome_unknown",
            ):
                continue
            terminal = parse_contract(
                account(self.history.objects.get(event.payload_ref, ObjectType.EVENT)), EventPayload
            ).data
            item = parse_contract(encode(terminal.get("occurrence")), CallOccurrence)
            identity = (item.logical_step_id, item.call_index, item.attempt)
            if identity in completed:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源事件重复完成同一 occurrence")
            completed[identity] = (event.seq, terminal, event.type)
        recordings: list[Recording] = []
        seen: set[tuple[str, int, int]] = set()
        for event in indexed_events:
            if event.type not in ("llm.requested", "tool.started", "call.reused"):
                continue
            payload = parse_contract(
                account(self.history.objects.get(event.payload_ref, ObjectType.EVENT)), EventPayload
            )
            occurrence = parse_contract(encode(payload.data.get("occurrence")), CallOccurrence)
            key = (occurrence.logical_step_id, occurrence.call_index, occurrence.attempt)
            row = by_key.get(key)
            if row is None:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "调用 occurrence 没有完整录制结果")
            if key in seen:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源事件重复启动同一 occurrence")
            seen.add(key)
            call_id = str(row["id"])
            intent = parse_contract(
                self._owned(
                    repository,
                    row["intent_ref"],
                    ObjectType.CALL,
                    "external_call",
                    call_id,
                    "intent",
                ),
                CallIntent,
            )
            if (
                intent.occurrence != occurrence
                or intent.checkpoint != checkpoint_for_event[event.seq]
                or intent.effect.value != row["effect"]
                or intent.request_ref != row["request_digest"]
                or any(
                    getattr(occurrence, field) != row[field]
                    for field in (
                        "logical_step_id",
                        "call_index",
                        "attempt",
                        "provider",
                        "signature_digest",
                        "request_digest",
                    )
                )
                or (
                    event.type != "call.reused"
                    and (
                        payload.data.get("effect") != row["effect"]
                        or payload.data.get("request_ref") != intent.request_ref
                    )
                )
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "调用事件、索引与原意图不符")
            declared = (
                environment.model_signature.rsplit(":", 1)[-1] == occurrence.signature_digest
                if occurrence.provider.startswith("llm:")
                else occurrence.signature_digest in environment.tool_signatures
            )
            if not declared:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "冻结环境未声明原调用签名")
            terminal_record = completed.get(key)
            uncertain = terminal_record is not None and terminal_record[2] == "call.outcome_unknown"
            result_ref = row["result_ref"]
            if uncertain:
                assert terminal_record is not None
                result_ref = terminal_record[1].get("result_ref")
            if not isinstance(result_ref, str):
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "调用缺少完整结果或历史未知诊断")
            saved = parse_contract(
                self._owned(
                    repository,
                    result_ref,
                    ObjectType.CALL,
                    "external_call",
                    call_id,
                    "uncertain_diagnostic" if uncertain else "result",
                ),
                SavedCallOutcome,
            )
            if event.type == "call.reused":
                if payload.data.get("result_ref") != row["result_ref"]:
                    raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "复用事件与录制结果不符")
            elif (
                terminal_record is None
                or terminal_record[0] <= event.seq
                or terminal_record[1].get("result_ref") != result_ref
                or terminal_record[1].get("error") != saved.error
                or terminal_record[1].get("occurrence") != occurrence.model_dump(mode="json")
            ):
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "范围内没有匹配的调用完成事件")
            if uncertain:
                if (
                    event.type == "call.reused"
                    or saved.error is None
                    or saved.error["code"] != ErrorCode.OUTCOME_UNKNOWN.value
                    or row["status"] not in ("succeeded", "outcome_unknown")
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "历史未知诊断与调用状态不符")
            elif row["status"] != ("failed" if saved.error else "succeeded"):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "调用状态与保存结果不符")
            record_type = UncertainRecording if uncertain else Recording
            record = record_type(
                intent,
                saved,
                self._owned(
                    repository,
                    intent.request_ref,
                    ObjectType.BLOB,
                    "external_call",
                    call_id,
                    "request",
                ),
                self._owned(
                    repository,
                    saved.response_ref,
                    ObjectType.BLOB,
                    "external_call",
                    call_id,
                    "response",
                ),
                call_id,
                event.type == "call.reused",
            )
            record.outcome()
            account(record.request)
            account(record.response)
            physical = self._physical(repository, row, family)
            if occurrence.provider in SANDBOX_PROVIDERS and not uncertain:
                with self.history.engine.connect() as connection:
                    rejected = (
                        connection.execute(
                            sa.select(TABLES["hybrid_observations"].c.kind).where(
                                TABLES["hybrid_observations"].c.repository_id == repository,
                                TABLES["hybrid_observations"].c.call_id == physical["id"],
                            )
                        ).scalar_one_or_none()
                        == "miss"
                    )
                if not rejected:
                    self._execution(repository, physical, record)
            recordings.append(record)
        if len({item.at_commit for item in settlements}) != len(settlements):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "未知检查点包含重复可信核对事件")
        return RecordedBundle(
            selection,
            environment,
            tuple(checkpoints),
            tuple(recordings),
            tuple(discards),
            settlements=tuple(settlements),
            unknown_stops=tuple(unknown_stops),
        )
