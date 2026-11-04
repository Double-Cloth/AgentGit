"""执行证据的受控只读视图；不初始化执行器，不接受任意对象引用。"""

from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, decode
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
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.replay.recorded import Recording, UncertainRecording
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.inspection import InspectionService
from agentgit.services.recordings import RecordingReader

ExecutionResource = Literal[
    "command", "code", "stdin", "stdout", "stderr", "metadata", "input_manifest", "output_manifest"
]


class ExecutionViewService:
    def __init__(self, history: HistoryService):
        self.history, self.reader = history, RecordingReader(history)

    def list(
        self, repository: UUID, run: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        InspectionService._limit(limit)
        calls = TABLES["external_calls"]
        order = (calls.c.logical_step_id, calls.c.call_index, calls.c.attempt)
        with self.history.engine.connect() as connection:
            self.history._run(connection, repository, run)
            statement = sa.select(calls).where(
                calls.c.repository_id == repository,
                calls.c.run_id == run,
                calls.c.provider.in_(SANDBOX_PROVIDERS),
            )
            if after_id is not None:
                anchor = (
                    connection.execute(
                        sa.select(calls).where(
                            calls.c.repository_id == repository,
                            calls.c.run_id == run,
                            calls.c.id == after_id,
                            calls.c.provider.in_(SANDBOX_PROVIDERS),
                        )
                    )
                    .mappings()
                    .first()
                )
                if anchor is None:
                    raise AgentGitError(ErrorCode.NOT_FOUND, "执行游标不属于当前 Run")
                statement = statement.where(
                    sa.tuple_(*order)
                    > sa.tuple_(anchor["logical_step_id"], anchor["call_index"], anchor["attempt"])
                )
            rows = list(connection.execute(statement.order_by(*order).limit(limit + 1)).mappings())
        return {
            "items": [
                {
                    key: row[key]
                    for key in (
                        "id",
                        "run_id",
                        "origin_call_id",
                        "logical_step_id",
                        "call_index",
                        "attempt",
                        "provider",
                        "status",
                    )
                }
                for row in rows[:limit]
            ],
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def _load(
        self, repository: UUID, run: UUID, identity: UUID
    ) -> tuple[dict[str, Any], dict[str, tuple[str, ObjectType, str, str, str]]]:
        calls, executions = TABLES["external_calls"], TABLES["sandbox_executions"]
        with self.history.engine.connect() as connection:
            self.history._run(connection, repository, run)
            logical = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository,
                        calls.c.run_id == run,
                        calls.c.id == identity,
                        calls.c.provider.in_(SANDBOX_PROVIDERS),
                    )
                )
                .mappings()
                .first()
            )
            if logical is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "执行调用不可访问")
            family = set(self.reader.versions._family(connection, repository, run))
        physical = self.reader._physical(repository, dict(logical), family)
        call_id = str(physical["id"])
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(executions).where(
                        executions.c.repository_id == repository,
                        executions.c.call_id == physical["id"],
                        executions.c.run_id == physical["run_id"],
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "调用缺少原物理执行索引")
        resources: dict[str, tuple[str, ObjectType, str, str, str]] = {}

        def owned(reference: str, kind: ObjectType, owner: str, owner_id: str, role: str) -> bytes:
            try:
                return self.reader._owned(repository, reference, kind, owner, owner_id, role)
            except AgentGitError as error:
                if error.code == ErrorCode.NOT_FOUND:
                    raise AgentGitError(ErrorCode.RECORDING_MISSING, "执行证据对象缺失") from None
                raise

        intent = parse_contract(
            owned(physical["intent_ref"], ObjectType.CALL, "external_call", call_id, "intent"),
            CallIntent,
        )
        occurrence = intent.occurrence
        if (
            intent.checkpoint != row["checkpoint_commit_id"]
            or intent.request_ref != physical["request_digest"]
            or intent.request_ref != row["request_digest"]
            or intent.effect.value != physical["effect"]
            or row["attempt"] != occurrence.attempt
            or any(
                getattr(occurrence, key) != physical[key]
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
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行索引与调用意图不符")
        raw_request = owned(
            intent.request_ref, ObjectType.BLOB, "external_call", call_id, "request"
        )
        arguments: TerminalArguments | CodeArguments
        if occurrence.provider == "tool:terminal_exec/v1":
            arguments = parse_contract(raw_request, TerminalArguments)
        else:
            arguments = parse_contract(raw_request, CodeArguments)
        policy = parse_contract(
            owned(row["policy_ref"], ObjectType.BLOB, "sandbox_execution", call_id, "policy"),
            ExecutionPolicy,
        )
        if (
            arguments.execution_policy_ref != row["policy_ref"]
            or policy.digest() != row["policy_ref"]
            or arguments.input_manifest_ref != row["input_manifest_ref"]
            or row["image_id"] != policy.image_id
            or occurrence.signature_digest != execution_signature(occurrence.provider, policy)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行请求与固定策略不符")
        checkpoint = row["checkpoint_commit_id"]
        input_owner = ("sandbox_input", str(physical["run_id"]), checkpoint)
        resources["input_manifest"] = (
            row["input_manifest_ref"],
            ObjectType.ARTIFACT_MANIFEST,
            *input_owner,
        )
        inputs = parse_contract(owned(*resources["input_manifest"]), ExecutionManifest)
        if isinstance(arguments, TerminalArguments):
            resources["command"] = (arguments.command_ref, ObjectType.BLOB, *input_owner)
        elif arguments.code_ref is not None:
            resources["code"] = (arguments.code_ref, ObjectType.BLOB, *input_owner)
        else:
            path = (
                arguments.script_path
                if arguments.cwd == "."
                else f"{arguments.cwd}/{arguments.script_path}"
            )
            script = next((item for item in inputs.files if item.path == path), None)
            if script is None:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行脚本不属于原输入清单")
            resources["code"] = (script.object_ref, ObjectType.BLOB, *input_owner)
        if arguments.stdin_ref is not None:
            resources["stdin"] = (arguments.stdin_ref, ObjectType.BLOB, *input_owner)
        for name in ("command", "code", "stdin"):
            if name in resources:
                owned(*resources[name])
        result, metadata = None, None
        if row["response_ref"] is not None:
            result_ref = physical["result_ref"]
            diagnostic = result_ref is None
            if diagnostic:
                refs = TABLES["object_refs"]
                with self.history.engine.connect() as connection:
                    found = list(
                        connection.execute(
                            sa.select(refs.c.object_id).where(
                                refs.c.repository_id == repository,
                                refs.c.owner_type == "external_call",
                                refs.c.owner_id == call_id,
                                refs.c.role == "uncertain_diagnostic",
                            )
                        ).scalars()
                    )
                if len(found) != 1 or physical["status"] != "outcome_unknown":
                    raise AgentGitError(ErrorCode.RECORDING_MISSING, "未知执行没有唯一诊断证据")
                result_ref = found[0]
            saved = parse_contract(
                owned(
                    result_ref,
                    ObjectType.CALL,
                    "external_call",
                    call_id,
                    "uncertain_diagnostic" if diagnostic else "result",
                ),
                SavedCallOutcome,
            )
            if saved.response_ref != row["response_ref"] or (
                not diagnostic and row["result_ref"] != result_ref
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行回执索引与保存结果不符")
            response = owned(
                saved.response_ref, ObjectType.BLOB, "external_call", call_id, "response"
            )
            record = (UncertainRecording if diagnostic else Recording)(
                intent, saved, raw_request, response, call_id
            )
            record.outcome()
            try:
                self.reader._execution(repository, physical, record)
            except AgentGitError as error:
                if error.code == ErrorCode.NOT_FOUND:
                    raise AgentGitError(ErrorCode.RECORDING_MISSING, "执行证据对象缺失") from None
                raise
            result = parse_contract(response, ExecutionResult)
            if physical["status"] != (
                "outcome_unknown" if diagnostic else "failed" if saved.error else "succeeded"
            ) or row["status"] != (
                "outcome_unknown" if result.error_code == "outcome_unknown" else "complete"
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行状态与原回执不符")
            for name, reference in (
                ("stdout", result.stdout_ref),
                ("stderr", result.stderr_ref),
                ("output_manifest", result.output_manifest_ref),
                ("metadata", row["metadata_ref"]),
            ):
                if reference is not None:
                    resources[name] = (
                        reference,
                        ObjectType.ARTIFACT_MANIFEST
                        if name == "output_manifest"
                        else ObjectType.BLOB,
                        "sandbox_execution",
                        call_id,
                        "evidence",
                    )
            if "metadata" not in resources:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "执行缺少采集元数据")
            metadata = decode(owned(*resources["metadata"]))
            if (
                not isinstance(metadata, dict)
                or type(metadata.get("schema_version")) is not int
                or metadata["schema_version"] != 1
                or type(metadata.get("wall_ms")) is not int
                or not 0 <= metadata["wall_ms"] <= 9_007_199_254_740_991
                or metadata.get("container_name") != row["container_name"]
                or metadata.get("cleanup_confirmed") is not result.cleanup_confirmed
                or (
                    metadata.get("container_id") is not None
                    and metadata["container_id"] != row["container_id"]
                )
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行采集元数据不符")
        elif row["status"] == "complete" or row["result_ref"] is not None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "完整执行缺少响应证据")
        return (
            {
                "call_id": identity,
                "run_id": run,
                "physical_call_id": physical["id"],
                "physical_run_id": physical["run_id"],
                "provider": occurrence.provider,
                "logical_step_id": occurrence.logical_step_id,
                "attempt": occurrence.attempt,
                "call_status": physical["status"],
                "execution_status": row["status"],
                "checkpoint_commit_id": checkpoint,
                "arguments": arguments.model_dump(mode="json"),
                "result": result.model_dump(mode="json") if result is not None else None,
                "wall_ms": metadata["wall_ms"] if metadata is not None else None,
                "cleanup_confirmed": row["cleanup_confirmed"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
                "resources": sorted(resources),
            },
            resources,
        )

    def detail(self, repository: UUID, run: UUID, identity: UUID) -> dict[str, Any]:
        return self._load(repository, run, identity)[0]

    def content(
        self,
        repository: UUID,
        run: UUID,
        identity: UUID,
        resource: ExecutionResource,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> tuple[dict[str, Any], bytes]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1_048_576
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "执行证据分段范围无效")
        _, resources = self._load(repository, run, identity)
        binding = resources.get(resource)
        if binding is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "该执行没有此类已发布证据")
        reference, kind, owner, owner_id, role = binding
        try:
            raw = self.reader._owned(repository, reference, kind, owner, owner_id, role)
        except AgentGitError as error:
            if error.code == ErrorCode.NOT_FOUND:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "执行证据对象缺失") from None
            raise
        if offset > len(raw):
            raise AgentGitError(ErrorCode.VALIDATION, "执行证据偏移超过内容长度")
        end = min(len(raw), offset + limit)
        return {
            "reference": reference,
            "total_bytes": len(raw),
            "offset": offset,
            "next_offset": end if end < len(raw) else None,
        }, raw[offset:end]
