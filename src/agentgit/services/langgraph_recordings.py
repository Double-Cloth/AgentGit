"""固定图的严格 Recorded 只读准备：来源、控制、调用与逻辑产物共同固定。"""

import platform
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa

from agentgit import __version__
from agentgit.adapters.langgraph import LangGraphAdapter
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import LangGraphProgram
from agentgit.core.langgraph_recorded import (
    LangGraphRecordedControl,
    LangGraphRecordedSpec,
    LangGraphRecordingBundle,
)
from agentgit.core.langgraph_runs import LangGraphJobPayload, LangGraphRunState
from agentgit.core.mcp_schema import validate_value
from agentgit.core.models import (
    CallIntent,
    CommitManifest,
    EffectClass,
    EnvironmentManifest,
    RunStatus,
    parse_contract,
)
from agentgit.core.research import SavedCallOutcome
from agentgit.core.versions import RecordedRange
from agentgit.replay.recorded import RecordedAdapter, Recording
from agentgit.runtime.langgraph_checkpoints import LangGraphCheckpointJournal
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.versions import VersionService


def projection_digest(state: LangGraphRunState) -> str:
    return object_digest(ObjectType.BLOB, encode(state.logical_projection()))


def _mismatch(message: str) -> AgentGitError:
    return AgentGitError(ErrorCode.REPLAY_MISMATCH, message)


@dataclass(frozen=True)
class PreparedLangGraphRecording:
    program: LangGraphProgram
    bundle: LangGraphRecordingBundle
    before: LangGraphRunState
    after: LangGraphRunState
    native_raw: bytes | None
    record: Recording


def _owned_input(history: HistoryService, repository: UUID, reference: str) -> bytes:
    refs = TABLES["object_refs"]
    with history.engine.connect() as connection:
        row = connection.execute(
            sa.select(refs.c.object_id).where(
                refs.c.repository_id == repository,
                refs.c.object_id == reference,
                refs.c.owner_type == "repository",
                refs.c.role == "input",
            )
        ).first()
    if row is None:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "图录制输入缺少同项目登记")
    return history.objects.get(reference, ObjectType.BLOB)


def load_recording(
    history: HistoryService,
    repository: UUID,
    spec: LangGraphRecordedSpec,
    *,
    seen: tuple[UUID, ...] = (),
) -> PreparedLangGraphRecording:
    bundle = parse_contract(
        _owned_input(history, repository, spec.bundle_ref), LangGraphRecordingBundle
    )
    if (bundle.source_run_id, bundle.from_commit, bundle.to_commit) != (
        spec.source_run_id,
        spec.from_commit,
        spec.to_commit,
    ):
        raise _mismatch("图录制范围与固定说明不符")
    result = prepare_recording(
        history, repository, UUID(spec.source_run_id), spec.from_commit, spec.to_commit, seen=seen
    )
    if result.bundle != bundle:
        raise _mismatch("图录制来源、控制或输入已不匹配")
    return result


def prepare_recording(
    history: HistoryService,
    repository: UUID,
    source: UUID,
    first: str,
    last: str,
    *,
    seen: tuple[UUID, ...] = (),
) -> PreparedLangGraphRecording:
    if source in seen or len(seen) >= 100:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "图 Recorded 来源链循环或过长")
    seen = (*seen, source)
    code, lock = source_revision(), dependency_lock_digest()
    versions = VersionService(history)
    selection = versions.select_range(
        repository, RecordedRange(source_run_id=str(source), from_commit=first, until_commit=last)
    )
    with history.engine.connect() as connection:
        run = dict(history._run(connection, repository, source))
        job = (
            connection.execute(
                sa.select(TABLES["jobs"]).where(
                    TABLES["jobs"].c.repository_id == repository, TABLES["jobs"].c.run_id == source
                )
            )
            .mappings()
            .first()
        )
        links = list(
            connection.execute(
                sa.select(TABLES["run_commits"])
                .where(
                    TABLES["run_commits"].c.repository_id == repository,
                    TABLES["run_commits"].c.run_id == source,
                )
                .order_by(TABLES["run_commits"].c.run_order)
                .limit(10001)
            ).mappings()
        )
        rows = list(
            connection.execute(
                sa.select(TABLES["events"])
                .where(
                    TABLES["events"].c.repository_id == repository,
                    TABLES["events"].c.run_id == source,
                    TABLES["events"].c.seq <= selection.until_seq,
                )
                .order_by(TABLES["events"].c.seq)
                .limit(20001)
            ).mappings()
        )
    if job is None or job["kind"] != "langgraph_run" or len(links) > 10000 or len(rows) > 20000:
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图来源不存在或超过有界录制范围")
    payload = parse_contract(
        history.objects.get(job["payload_ref"], ObjectType.BLOB), LangGraphJobPayload
    )
    if payload.code_revision != code or payload.dependency_lock_digest != lock:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "图录制源码或锁与当前不符")
    program = parse_contract(
        _owned_input(history, repository, payload.program_ref), LangGraphProgram
    )
    if payload.max_wall_ms != program.limits.max_wall_ms:
        raise _mismatch("图来源墙钟与程序不符")
    parent = (
        load_recording(history, repository, payload.replay, seen=seen) if payload.replay else None
    )
    origin = (
        payload.fork.source_run_id
        if payload.fork
        else payload.replay.source_run_id
        if payload.replay
        else None
    )
    if (
        run["origin_run_id"] != (UUID(origin) if origin else None)
        or job["origin_run_id"] != run["origin_run_id"]
        or run["mode"] != ("recorded" if payload.replay else "live")
    ):
        raise _mismatch("图来源模式或初始化索引不符")
    if origin is not None:
        with history.engine.connect() as connection:
            forks = TABLES["branch_forks"]
            branch = (
                connection.execute(
                    sa.select(forks).where(
                        forks.c.repository_id == repository,
                        forks.c.branch_name == run["branch_name"],
                    )
                )
                .mappings()
                .first()
            )
        parent_spec = payload.fork or payload.replay
        assert parent_spec is not None
        if (
            branch is None
            or branch["source_run_id"] != UUID(origin)
            or branch["from_commit_id"] != parent_spec.from_commit
            or run["base_commit_id"] != parent_spec.from_commit
        ):
            raise _mismatch("图来源分支与固定范围不符")
    events = {row["seq"]: row for row in rows}
    if [row["seq"] for row in rows] != list(range(1, selection.until_seq + 1)):
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图来源事件不完整")
    bodies = {
        row["seq"]: history.objects.get_json(row["payload_ref"], ObjectType.EVENT)["data"]
        for row in rows
    }
    states: dict[str, LangGraphRunState] = {}
    manifests: dict[str, CommitManifest] = {}
    applied: dict[str, list[Any]] = {}
    snapshots: dict[str, Any] = {}
    source_path = [row["commit_id"] for row in links]
    if last not in source_path:
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图终点必须有来源自身的检查点")
    source_path = source_path[: source_path.index(last) + 1]
    for cid in source_path:
        snapshot = history.restore(repository, cid)
        state = parse_contract(encode(snapshot.data), LangGraphRunState)
        manifest = parse_contract(history.objects.get(cid, ObjectType.COMMIT), CommitManifest)
        environment = parse_contract(
            history.objects.get(manifest.environment_ref, ObjectType.ENVIRONMENT),
            EnvironmentManifest,
        )
        if (
            state.program_ref != payload.program_ref
            or environment.input_ref != state.program_ref
            or environment.code_revision != code
            or environment.dependency_lock_digest != lock
            or environment.model_signature != "no-model/langgraph-fixed/v1"
            or environment.runtime_version != __version__
            or environment.python_version != platform.python_version()
            or environment.prompt_digest
            != object_digest(ObjectType.BLOB, b"mcp-read-review-publish/v1")
            or environment.evaluation_time != "2026-10-20T00:00:00Z"
            or environment.random_seed != 0
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "图检查点环境或程序不符")
        logical = history.objects.get_json(manifest.event_manifest_ref, ObjectType.EVENT_MANIFEST)[
            "events"
        ]
        for item in logical:
            row = events.get(item["seq"])
            if (
                row is None
                or row["payload_ref"] != item["payload_ref"]
                or row["type"] != item["type"]
                or str(row["run_id"]) != str(source)
            ):
                raise _mismatch("图事件清单与来源索引不符")
        states[cid], manifests[cid], snapshots[cid], applied[cid] = (
            state,
            manifest,
            snapshot,
            logical,
        )
    after = states[last]
    if after.status != RunStatus.COMPLETED or after.trace is None:
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图录制终点必须完整成功")
    start = source_path[0] if first == run["base_commit_id"] else first
    if start not in states:
        raise _mismatch("图起点不属于明确来源路径")
    before = states[start]
    if (
        before.status
        in {RunStatus.OUTCOME_UNKNOWN, RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED}
        or before.error_code is not None
    ):
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图起点包含未决结果")
    family: list[UUID] = []
    current: UUID | None = source
    with history.engine.connect() as connection:
        while current is not None:
            if current in family or len(family) >= 100:
                raise _mismatch("图调用来源链不合法")
            family.append(current)
            ancestor_run = history._run(connection, repository, current)
            ancestor_job = (
                connection.execute(
                    sa.select(TABLES["jobs"]).where(
                        TABLES["jobs"].c.repository_id == repository,
                        TABLES["jobs"].c.run_id == current,
                    )
                )
                .mappings()
                .first()
            )
            if ancestor_job is None or ancestor_job["kind"] != "langgraph_run":
                raise _mismatch("图调用来源缺少固定图 Job")
            ancestor = parse_contract(
                history.objects.get(ancestor_job["payload_ref"], ObjectType.BLOB),
                LangGraphJobPayload,
            )
            ancestor_origin = (
                ancestor.fork.source_run_id
                if ancestor.fork
                else ancestor.replay.source_run_id
                if ancestor.replay
                else None
            )
            if (
                ancestor.code_revision != code
                or ancestor.dependency_lock_digest != lock
                or ancestor_run["origin_run_id"]
                != (UUID(ancestor_origin) if ancestor_origin else None)
                or ancestor_job["origin_run_id"] != ancestor_run["origin_run_id"]
            ):
                raise _mismatch("图调用来源身份或环境不匹配")
            current = UUID(ancestor_origin) if ancestor_origin else None
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"])
                .where(
                    TABLES["external_calls"].c.repository_id == repository,
                    TABLES["external_calls"].c.run_id.in_(family),
                )
                .limit(101)
            ).mappings()
        )
    if len(calls) != 1 or calls[0]["result_ref"] is None or calls[0]["status"] != "succeeded":
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图缺少唯一完整的取证录制")
    call = calls[0]
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    saved = parse_contract(
        history.objects.get(call["result_ref"], ObjectType.CALL), SavedCallOutcome
    )
    record = Recording(
        intent,
        saved,
        history.objects.get(intent.request_ref, ObjectType.BLOB),
        history.objects.get(saved.response_ref, ObjectType.BLOB),
        str(call["id"]),
    )
    outcome = record.outcome()
    binding = program.binding
    if (
        outcome.error is not None
        or outcome.trace != after.trace
        or intent.effect != EffectClass.READ_ONLY
        or intent.occurrence.provider != binding.provider
        or intent.occurrence.signature_digest != binding.signature()
        or intent.occurrence.logical_step_id != "fetch"
        or intent.occurrence.call_index != 0
        or intent.occurrence.attempt != 1
        or intent.max_response_bytes != binding.server.max_response_bytes
        or record.request != encode(program.request.model_dump(mode="json"))
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
        or call["effect"] != intent.effect.value
    ):
        raise _mismatch("图取证录制与固定策略或轨迹不符")
    value = decode(record.response)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("kind") != "mcp_tool_result/v1"
        or value.get("server_identity") != binding.server.server_identity
        or value.get("tool_name") != binding.tool_name
        or value.get("schema_digest") != binding.schema_digest()
        or value.get("arguments_digest") != intent.request_ref
        or value.get("effect_class") != "READ_ONLY"
        or value.get("cancellation") != "transport_only"
        or after.values.get("fetch_ref") != outcome.trace.response_ref
        or not isinstance(value.get("content"), list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"type", "text"}
            or item.get("type") != "text"
            or not isinstance(item.get("text"), str)
            for item in value["content"]
        )
        or after.values.get("fetch_text") != "\n".join(item["text"] for item in value["content"])
    ):
        raise _mismatch("图保存事实与工具响应不符")
    if "outputSchema" in binding.definition():
        validate_value(binding.definition()["outputSchema"], value.get("structuredContent"))
    if before.trace is not None and before.trace != outcome.trace:
        raise _mismatch("图起点取证来源不符")
    needs_fetch = before.trace is None
    if needs_fetch and parent is None and intent.checkpoint not in selection.commits:
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图范围未覆盖取证意图检查点")
    if parent is not None and (
        parent.bundle.intent_ref != call["intent_ref"]
        or parent.bundle.result_ref != call["result_ref"]
    ):
        raise _mismatch("复合图录制与原调用不符")
    native_raw = _native_input(history, program, source, before, snapshots[start])
    _native_input(history, program, source, after, snapshots[last])
    if payload.fork is not None:
        # Fork 和 Recorded 的来源证明可交替；延迟导入避免服务模块初始化成环。
        from agentgit.services.langgraph_forks import load_fork_source

        parent_program, parent_state, parent_raw = load_fork_source(
            history, repository, UUID(payload.fork.source_run_id), payload.fork.from_commit
        )
        if (
            program != parent_program.model_copy(update={"review_note": program.review_note})
            or before.trace != parent_state.trace
        ):
            raise _mismatch("图 Fork 录制输入来源不符")
    if start == source_path[0] and first == run["base_commit_id"]:
        expected_type = (
            "langgraph.fork_seeded"
            if payload.fork
            else "langgraph.recorded_initialized"
            if payload.replay
            else "langgraph.initialized"
        )
        if not any(e["type"] == expected_type for e in applied[start]):
            raise _mismatch("图初始化没有完整来源事件")
        if payload.fork is not None:
            expected = parent_state.model_copy(
                update={
                    "program_ref": payload.program_ref,
                    "status": RunStatus.RUNNING,
                    "review_boundary_reached": True,
                    "fork_initialized": False,
                }
            )
            expected_native: bytes | None = LangGraphCheckpointJournal(
                payload.fork.source_run_id,
                lambda *args: None,
                restored=parent_raw,
            ).clone_thread(str(source))
            if (
                before.logical_projection() != expected.logical_projection()
                or native_raw != expected_native
                or before.actual_attempts != 0
            ):
                raise _mismatch("图 Fork 起点不是确切的来源克隆")
        elif parent is not None:
            expected_native = (
                None
                if parent.native_raw is None
                else LangGraphCheckpointJournal(
                    parent.bundle.source_run_id,
                    lambda *args: None,
                    restored=parent.native_raw,
                ).clone_thread(str(source))
            )
            if (
                before.logical_projection() != parent.before.logical_projection()
                or native_raw != expected_native
                or before.actual_attempts != 0
                or before.replay_control_index != 0
            ):
                raise _mismatch("图 Recorded 起点不是确切的来源克隆")
        elif before != LangGraphRunState(program_ref=payload.program_ref):
            raise _mismatch("图 Live 起点不是固定的空初始状态")
    pairs = _controls(source_path, states, applied, rows, bodies, parent)
    start_index = source_path.index(start)
    controls = []
    for pause_index, resume_index, item in pairs:
        if resume_index <= start_index:
            continue
        if pause_index <= start_index:
            if before.status != RunStatus.PAUSED or projection_digest(before) != item.paused_digest:
                raise _mismatch("图起点暂停依据不匹配")
            item = item.model_copy(update={"kind": "initial", "before_digest": None})
        controls.append(item)
    if len(controls) > 64:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "图录制控制超过上限")
    report = after.values["report"].encode()
    artifacts = snapshots[last].artifacts
    if {a.path for a in artifacts} != {"langgraph-native.json", "report.md"} or not any(
        a.path == "report.md"
        and a.media_type == "text/markdown"
        and a.object_ref == object_digest(ObjectType.BLOB, report)
        and history.objects.get(a.object_ref, ObjectType.BLOB) == report
        for a in artifacts
    ):
        raise _mismatch("图终点报告产物与逻辑结果不符")
    bundle = LangGraphRecordingBundle(
        source_run_id=str(source),
        from_commit=first,
        to_commit=last,
        start_commit=start,
        program_ref=payload.program_ref,
        source_payload_ref=job["payload_ref"],
        from_state_ref=parse_contract(
            history.objects.get(first, ObjectType.COMMIT), CommitManifest
        ).state_ref,
        start_state_ref=manifests[start].state_ref,
        to_state_ref=manifests[last].state_ref,
        environment_ref=manifests[start].environment_ref,
        call_id=str(call["id"]),
        intent_ref=call["intent_ref"],
        result_ref=call["result_ref"],
        needs_fetch=needs_fetch,
        needs_fork_initialization=(
            payload.fork is not None
            or (parent is not None and parent.bundle.needs_fork_initialization)
        )
        and not before.fork_initialized,
        controls=tuple(controls),
    )
    return PreparedLangGraphRecording(program, bundle, before, after, native_raw, record)


def _native_input(
    history: HistoryService,
    program: LangGraphProgram,
    source: UUID,
    state: LangGraphRunState,
    snapshot: Any,
) -> bytes | None:
    if state.native_ref is None:
        if state.values or state.native_next:
            raise _mismatch("图起点缺少原生输入")
        return None
    if not any(
        a.path == "langgraph-native.json"
        and a.object_ref == state.native_ref
        and a.media_type == "application/json"
        for a in snapshot.artifacts
    ):
        raise _mismatch("图原生输入缺少产物引用")
    raw = history.objects.get(state.native_ref, ObjectType.BLOB)
    journal = LangGraphCheckpointJournal(str(source), lambda *args: None, restored=raw)
    native = LangGraphAdapter(
        program,
        RecordedAdapter((), lambda: None),
        thread_id=str(source),
        checkpointer=journal,
        step_boundaries=True,
    ).state()
    if dict(native.values) != state.values or tuple(native.next) != state.native_next:
        raise _mismatch("图原生输入与逻辑状态不符")
    return raw


def _controls(
    path: list[str],
    states: dict[str, LangGraphRunState],
    applied: dict[str, list[Any]],
    rows: list[Any],
    bodies: dict[int, Any],
    parent: PreparedLangGraphRecording | None,
) -> list[tuple[int, int, LangGraphRecordedControl]]:
    requests = {row["seq"]: row for row in rows if row["type"] == "control.requested"}
    pairs: list[tuple[int, int, LangGraphRecordedControl]] = []
    pending: (
        tuple[
            int,
            LangGraphRunState | None,
            LangGraphRunState,
            Any,
            Literal["native", "initial", "requested"],
            str | None,
        ]
        | None
    ) = None
    previous: LangGraphRunState | None = None
    last_resume_seq = 0
    recorded_pause_index: int | None = None
    for index, cid in enumerate(path):
        state = states[cid]
        if state.status == RunStatus.PAUSED and (
            previous is None or previous.status != RunStatus.PAUSED
        ):
            event = next(
                (
                    e
                    for e in applied[cid]
                    if e["type"]
                    in {
                        "langgraph.native_paused",
                        "langgraph.control_applied",
                        "langgraph.recorded_initialized",
                    }
                ),
                None,
            )
            if event is None:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "图暂停缺少应用事件")
            body = next(bodies[e["seq"]] for e in applied[cid] if e == event)
            recorded = body.get("recorded_control")
            kind: Literal["native", "initial", "requested"] = (
                "native"
                if event["type"] == "langgraph.native_paused"
                else "initial"
                if previous is None
                else "requested"
            )
            pause_request = None
            recorded_pause_index = None
            if recorded is not None:
                inherited = _parent_control(parent, recorded)
                recorded_pause_index = recorded["index"]
                if (
                    projection_digest(state) != inherited.paused_digest
                    or recorded["index"] != state.replay_control_index
                ):
                    raise _mismatch("图回放暂停投影不符")
                pause_request = inherited.pause_request_ref
            elif kind == "requested":
                candidates = [
                    r
                    for seq, r in requests.items()
                    if last_resume_seq < seq < event["seq"] and bodies[seq].get("action") == "pause"
                ]
                if not candidates:
                    raise AgentGitError(ErrorCode.RECORDING_MISSING, "图暂停缺少请求依据")
                pause_request = candidates[-1]["payload_ref"]
            elif previous is None and parent is not None:
                if state.replay_control_index >= len(parent.bundle.controls):
                    raise _mismatch("图初始暂停缺少父控制")
                inherited = parent.bundle.controls[state.replay_control_index]
                if (
                    inherited.kind != "initial"
                    or projection_digest(state) != inherited.paused_digest
                ):
                    raise _mismatch("图初始暂停依据不符")
                pause_request = inherited.pause_request_ref
                recorded_pause_index = state.replay_control_index
            pending = index, previous, state, event, kind, pause_request
        elif (
            state.status == RunStatus.RUNNING
            and previous is not None
            and previous.status == RunStatus.PAUSED
        ):
            event = next((e for e in applied[cid] if e["type"] == "langgraph.resumed"), None)
            if event is None or pending is None:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "图恢复缺少暂停/应用依据")
            body = bodies[event["seq"]]
            recorded = body.get("recorded_control")
            if recorded is not None:
                inherited = _parent_control(parent, recorded)
                if (
                    recorded["index"] != recorded_pause_index
                    or state.replay_control_index != recorded["index"] + 1
                ):
                    raise _mismatch("图回放恢复不能借用另一暂停的父控制")
                if projection_digest(state) != inherited.resumed_digest:
                    raise _mismatch("图回放恢复投影不符")
                resume_request = inherited.resume_request_ref
            else:
                candidates = [
                    r
                    for seq, r in requests.items()
                    if pending[3]["seq"] < seq < event["seq"]
                    and bodies[seq].get("action") == "resume"
                ]
                if not candidates:
                    raise AgentGitError(ErrorCode.RECORDING_MISSING, "图恢复缺少请求依据")
                resume_request = candidates[-1]["payload_ref"]
            pause_index, before, paused, pause_event, kind, pause_request = pending
            control = LangGraphRecordedControl(
                kind=kind,
                before_digest=projection_digest(before) if before is not None else None,
                paused_digest=projection_digest(paused),
                resumed_digest=projection_digest(state),
                pause_event_ref=pause_event["payload_ref"],
                pause_request_ref=pause_request,
                resume_event_ref=event["payload_ref"],
                resume_request_ref=resume_request,
            )
            pairs.append((pause_index, index, control))
            last_resume_seq = event["seq"]
            pending = None
        previous = state
    if pending is not None:
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "图录制暂停尚未恢复")
    return pairs


def _parent_control(
    parent: PreparedLangGraphRecording | None, recorded: Any
) -> LangGraphRecordedControl:
    if (
        parent is None
        or not isinstance(recorded, dict)
        or set(recorded) != {"bundle_ref", "index"}
        or type(recorded.get("index")) is not int
        or not 0 <= recorded["index"] < len(parent.bundle.controls)
        or recorded["bundle_ref"]
        != object_digest(ObjectType.BLOB, encode(parent.bundle.model_dump(mode="json")))
    ):
        raise _mismatch("图回放控制必须绑定确切的父范围说明及整数索引")
    return parent.bundle.controls[recorded["index"]]
