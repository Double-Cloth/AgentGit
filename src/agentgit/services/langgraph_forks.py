"""只读核对安全图来源、实际取证录制及原生完整边界。"""

from uuid import UUID

import sqlalchemy as sa

from agentgit.adapters.langgraph import LangGraphAdapter
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import LangGraphProgram
from agentgit.core.langgraph_runs import LangGraphJobPayload, LangGraphRunState
from agentgit.core.mcp_schema import validate_value
from agentgit.core.models import (
    CallIntent,
    CommitManifest,
    EffectClass,
    EnvironmentManifest,
    parse_contract,
)
from agentgit.core.research import SavedCallOutcome
from agentgit.core.versions import RecordedRange
from agentgit.replay.recorded import RecordedAdapter, Recording
from agentgit.runtime.langgraph_checkpoints import LangGraphCheckpointJournal
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.versions import VersionService


def load_fork_source(
    history: HistoryService, repository_id: UUID, source: UUID, checkpoint: str
) -> tuple[LangGraphProgram, LangGraphRunState, bytes]:
    versions = VersionService(history)
    versions.select_range(
        repository_id,
        RecordedRange(source_run_id=str(source), from_commit=checkpoint, until_commit=checkpoint),
    )
    snapshot = history.restore(repository_id, checkpoint)
    state = parse_contract(encode(snapshot.data), LangGraphRunState)
    family: list[UUID] = []
    current: UUID | None = source
    selected_payload: LangGraphJobPayload | None = None
    with history.engine.connect() as connection:
        while current is not None:
            if current in family or len(family) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "LangGraph Fork 来源链循环或过长")
            family.append(current)
            run = history._run(connection, repository_id, current)
            job = (
                connection.execute(
                    sa.select(TABLES["jobs"]).where(
                        TABLES["jobs"].c.repository_id == repository_id,
                        TABLES["jobs"].c.run_id == current,
                    )
                )
                .mappings()
                .first()
            )
            if job is None or job["kind"] != "langgraph_run":
                raise AgentGitError(ErrorCode.FORBIDDEN, "Fork 来源必须属于固定图 Job")
            payload = parse_contract(
                history.objects.get(job["payload_ref"], ObjectType.BLOB), LangGraphJobPayload
            )
            if (
                payload.code_revision != source_revision()
                or payload.dependency_lock_digest != dependency_lock_digest()
            ):
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "Fork 来源图的源码或锁不匹配")
            if selected_payload is None:
                selected_payload = payload
            origin = UUID(payload.fork.source_run_id) if payload.fork is not None else None
            if run["origin_run_id"] != origin or job["origin_run_id"] != origin:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "图 Fork 载荷与来源索引不一致")
            current = origin
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"])
                .where(
                    TABLES["external_calls"].c.repository_id == repository_id,
                    TABLES["external_calls"].c.run_id.in_(family),
                )
                .limit(101)
            ).mappings()
        )
        refs = TABLES["object_refs"]
        owned = connection.execute(
            sa.select(refs.c.object_id).where(
                refs.c.repository_id == repository_id,
                refs.c.object_id == state.program_ref,
                refs.c.owner_type == "repository",
                refs.c.role == "input",
            )
        ).first()
    assert selected_payload is not None
    if owned is None or state.program_ref != selected_payload.program_ref:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "图来源程序与检查点不匹配")
    program = parse_contract(
        history.objects.get(state.program_ref, ObjectType.BLOB), LangGraphProgram
    )
    manifest = parse_contract(history.objects.get(checkpoint, ObjectType.COMMIT), CommitManifest)
    environment = parse_contract(
        history.objects.get(manifest.environment_ref, ObjectType.ENVIRONMENT), EnvironmentManifest
    )
    if (
        environment.input_ref != state.program_ref
        or environment.code_revision != source_revision()
        or environment.dependency_lock_digest != dependency_lock_digest()
        or environment.model_signature != "no-model/langgraph-fixed/v1"
        or selected_payload.max_wall_ms != program.limits.max_wall_ms
    ):
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "图来源检查点环境不匹配")
    if (
        state.native_ref is None
        or state.native_operation != "checkpoint"
        or state.native_next != ("review",)
        or state.pending
        or state.error_code is not None
        or state.values.get("error_code") is not None
        or state.values.get("node_order") != ["fetch"]
        or state.values.get("reviewed_text") != ""
        or state.values.get("report") != ""
        or state.usage.steps != 1
        or state.usage.tool_calls != 1
        or state.trace is None
        or state.trace.error_code is not None
        or (selected_payload.fork is not None and not state.fork_initialized)
        or state.values.get("review_note") != program.review_note
        or len(calls) != 1
        or calls[0]["result_ref"] is None
    ):
        raise AgentGitError(ErrorCode.FORBIDDEN, "图 Fork 仅允许已完成取证的完整 review 检查点")
    call = calls[0]
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    saved = parse_contract(
        history.objects.get(call["result_ref"], ObjectType.CALL), SavedCallOutcome
    )
    request = encode(program.request.model_dump(mode="json"))
    response = history.objects.get(saved.response_ref, ObjectType.BLOB)
    record = Recording(
        intent,
        saved,
        history.objects.get(intent.request_ref, ObjectType.BLOB),
        response,
        str(call["id"]),
    )
    outcome = record.outcome()
    binding = program.binding
    if (
        call["status"] != "succeeded"
        or intent.effect != EffectClass.READ_ONLY
        or outcome.error is not None
        or outcome.trace != state.trace
        or state.values.get("fetch_ref") != outcome.trace.response_ref
        or intent.request_ref != object_digest(ObjectType.BLOB, request)
        or record.request != request
        or intent.occurrence.logical_step_id != "fetch"
        or intent.occurrence.call_index != 0
        or intent.occurrence.attempt != 1
        or intent.occurrence.provider != binding.provider
        or intent.occurrence.signature_digest != binding.signature()
        or intent.max_response_bytes != binding.server.max_response_bytes
        or intent.checkpoint not in versions.ancestors(repository_id, checkpoint)
        or any(
            getattr(intent.occurrence, key) != call[key]
            for key in (
                "logical_step_id",
                "call_index",
                "attempt",
                "provider",
                "signature_digest",
                "request_digest",
            )
        )
        or call["request_digest"] != intent.request_ref
        or call["effect"] != intent.effect.value
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图 Fork 取证录制、策略或轨迹不匹配")
    value = decode(response)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("kind") != "mcp_tool_result/v1"
        or value.get("server_identity") != binding.server.server_identity
        or value.get("tool_name") != binding.tool_name
        or value.get("schema_digest") != binding.schema_digest()
        or value.get("arguments_digest") != intent.request_ref
        or value.get("effect_class") != "READ_ONLY"
        or value.get("cancellation") != "transport_only"
        or not isinstance(value.get("content"), list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"type", "text"}
            or item.get("type") != "text"
            or not isinstance(item.get("text"), str)
            for item in value["content"]
        )
        or state.values.get("fetch_text") != "\n".join(item["text"] for item in value["content"])
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图 Fork 保存事实与原工具响应不符")
    definition = binding.definition()
    if "outputSchema" in definition:
        validate_value(definition["outputSchema"], value.get("structuredContent"))
    raw = history.objects.get(state.native_ref, ObjectType.BLOB)
    if not any(
        a.path == "langgraph-native.json" and a.object_ref == state.native_ref
        for a in snapshot.artifacts
    ):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "图 Fork 缺少原生日志产物引用")
    journal = LangGraphCheckpointJournal(str(source), lambda *args: None, restored=raw)
    native = LangGraphAdapter(
        program,
        RecordedAdapter((), lambda: None),
        thread_id=str(source),
        checkpointer=journal,
        step_boundaries=True,
    ).state()
    if dict(native.values) != state.values or tuple(native.next) != state.native_next:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "图原生检查点与外层状态不匹配")
    # 同时核对最后操作及当前 pending writes；这里只核对，不落目标数据。
    journal.require_review()
    return program, state, raw
