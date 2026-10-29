"""实际固定 SDK 的受限执行、严格录制、类型和遥测边界；尚非 Worker 验收。"""

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.adapters.langgraph import LangGraphAdapter, LangGraphAGJSONSerde
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import LangGraphDefinition, LangGraphProgram
from agentgit.core.mcp import MCPArguments, MCPBinding, MCPServer
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass
from agentgit.core.research import BudgetLimits, SavedCallOutcome
from agentgit.replay.recorded import RecordedAdapter, Recording
from agentgit.runtime.langgraph_checkpoints import LangGraphCheckpointJournal

FIXTURES = Path(__file__).parents[1] / "fixtures"


def program(**changes):
    tool = json.loads((FIXTURES / "mcp/v1/tools.json").read_text("utf-8"))
    binding = MCPBinding(
        server=MCPServer(server_identity="graph-fixture", endpoint="http://127.0.0.1:1234/mcp"),
        tool_name="echo",
        definition_json=encode(tool).decode(),
        effect_class=EffectClass.READ_ONLY,
    )
    graph = LangGraphDefinition.model_validate_json(
        (FIXTURES / "langgraph/v1/graph.json").read_bytes()
    )
    return LangGraphProgram(
        binding=binding,
        request=MCPArguments(arguments={"value": "graph-fixture-value"}),
        graph=graph,
        review_note="来源已核对",
        **changes,
    )


def recording(p, *, error=None, normalized=None):
    request = encode(p.request.model_dump(mode="json"))
    occurrence = CallOccurrence(
        logical_step_id="fetch",
        call_index=0,
        attempt=1,
        provider=p.binding.provider,
        signature_digest=p.binding.signature(),
        request_digest=object_digest(ObjectType.BLOB, request),
    )
    response = encode(
        normalized
        or {
            "schema_version": 1,
            "kind": "mcp_tool_result/v1",
            "server_identity": p.binding.server.server_identity,
            "tool_name": p.binding.tool_name,
            "schema_digest": p.binding.schema_digest(),
            "arguments_digest": occurrence.request_digest,
            "latency_ms": 1,
            "effect_class": "READ_ONLY",
            "cancellation": "transport_only",
            "content": [{"type": "text", "text": p.request.arguments["value"]}],
        }
    )
    intent = CallIntent(
        occurrence=occurrence,
        effect=EffectClass.READ_ONLY,
        request_ref=occurrence.request_digest,
        checkpoint="c" * 64,
        max_response_bytes=p.binding.server.max_response_bytes,
    )
    saved = SavedCallOutcome(
        response_ref=object_digest(ObjectType.BLOB, response),
        response_bytes=len(response),
        complete=True,
        error=error,
    )
    return Recording(intent, saved, request, response, "graph-recording")


def gateway(p, **kwargs):
    return RecordedAdapter((recording(p, **kwargs),), lambda: "c" * 64)


def forbidden(*args, **kwargs):
    raise AssertionError("测试路径禁止实际客户端、网络或子进程")


def test_actual_sdk_pause_and_resume_consumes_exactly_one_record_without_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "fixture-disabled-tracing-key")
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("agentgit.adapters.mcp.MCPAdapter.invoke", forbidden)
    p = program()
    calls = gateway(p)
    adapter = LangGraphAdapter(p, calls, thread_id=str(uuid4()))
    paused = adapter.invoke(start=True)
    assert paused["node_order"] == ["fetch"]
    assert paused["fetch_text"] == "graph-fixture-value"
    assert adapter.state().next == ("review",)
    final = adapter.invoke()
    assert final["node_order"] == ["fetch", "review", "publish"]
    assert final["report"] == "# 取证报告\n\ngraph-fixture-value\n\n审阅备注：来源已核对\n"
    assert adapter.state().next == () and calls.position == 1
    calls.assert_consumed()
    assert os.environ["LANGSMITH_TRACING"] == "true"


def test_start_resume_and_missing_record_guards_never_fall_back(monkeypatch):
    monkeypatch.setattr("agentgit.adapters.mcp.MCPAdapter.invoke", forbidden)
    p = program()
    adapter = LangGraphAdapter(p, RecordedAdapter((), lambda: "c" * 64), thread_id=str(uuid4()))
    with pytest.raises(AgentGitError) as caught:
        adapter.invoke()
    assert caught.value.code == ErrorCode.RECORDING_MISSING
    with pytest.raises(AgentGitError) as caught:
        adapter.invoke(start=True)
    assert caught.value.code == ErrorCode.RECORDING_MISSING
    with pytest.raises(AgentGitError) as caught:
        adapter.invoke(start=True)
    assert caught.value.code == ErrorCode.VERSION_CONFLICT


@pytest.mark.parametrize(
    "change", [{"template": "arbitrary"}, {"nodes": []}, {"edges": []}, {"pause_before": ["fetch"]}]
)
def test_unsupported_graph_is_rejected_before_execution(change):
    manifest = json.loads((FIXTURES / "langgraph/v1/graph.json").read_text("utf-8"))
    with pytest.raises((AgentGitError, ValidationError)):
        LangGraphDefinition.model_validate_json(encode({**manifest, **change}))


def test_external_effect_and_framework_mismatch_are_rejected(monkeypatch):
    p = program()
    binding = p.binding.model_dump(mode="json")
    binding["effect_class"] = "NON_IDEMPOTENT_WRITE"
    with pytest.raises(AgentGitError) as caught:
        LangGraphProgram(binding=MCPBinding.model_validate_json(encode(binding)), request=p.request)
    assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA
    monkeypatch.setattr("agentgit.adapters.langgraph.version", lambda name: "different")
    with pytest.raises(AgentGitError) as caught:
        LangGraphAdapter(p, gateway(p), thread_id=str(uuid4()))
    assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "value", [1.5, ("tuple",), {1: "key"}, b"bytes", object(), {"__reduce__": object()}, "\ud800"]
)
def test_serde_rejects_non_agjson_without_loading_python(value):
    with pytest.raises(AgentGitError) as caught:
        LangGraphAGJSONSerde().dumps_typed(value)
    assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "tag,raw",
    [
        ("pickle", b"cos\nsystem\n"),
        ("msgpack", b"data"),
        ("agjson-v1", b'{"x":1,"x":2}'),
        ("agjson-v1", b"1.5"),
    ],
)
def test_serde_never_accepts_type_tags_or_ambiguous_json(tag, raw):
    with pytest.raises(AgentGitError):
        LangGraphAGJSONSerde().loads_typed((tag, raw))


def test_serde_roundtrip_and_single_value_limit():
    serde = LangGraphAGJSONSerde()
    value = {"中文": [None, 0, True, "原生值"]}
    assert serde.loads_typed(serde.dumps_typed(value)) == value
    for action in [
        lambda: serde.dumps_typed("x" * 1048576),
        lambda: serde.loads_typed(("agjson-v1", b" " * 1048577)),
    ]:
        with pytest.raises(AgentGitError) as caught:
            action()
        assert caught.value.code == ErrorCode.OUTPUT_LIMIT


def test_tool_error_routes_to_end_without_report():
    p = program()
    adapter = LangGraphAdapter(
        p, gateway(p, error={"code": "tool_error", "message": "fixture"}), thread_id=str(uuid4())
    )
    values = adapter.invoke(start=True)
    assert values["error_code"] == "tool_error"
    assert values["node_order"] == ["fetch"] and values["report"] == ""
    assert adapter.state().next == ()


@pytest.mark.parametrize(
    "key,value",
    [
        ("server_identity", "other"),
        ("arguments_digest", "d" * 64),
        ("effect_class", "NON_IDEMPOTENT_WRITE"),
        ("content", [{"type": "image", "text": "payload"}]),
    ],
)
def test_recorded_normalized_result_cannot_change_provenance(key, value):
    p = program()
    normalized = decode(recording(p).response)
    normalized[key] = value
    adapter = LangGraphAdapter(p, gateway(p, normalized=normalized), thread_id=str(uuid4()))
    values = adapter.invoke(start=True)
    assert values["error_code"] == ErrorCode.REPLAY_MISMATCH.value
    assert adapter.state().next == () and values["report"] == ""


def test_internal_node_barriers_and_public_journal_roundtrip():
    p = program()
    calls = gateway(p)
    thread = str(uuid4())
    saved = []
    journal = LangGraphCheckpointJournal(
        thread, lambda raw, kind, native: saved.append((raw, kind, native))
    )
    adapter = LangGraphAdapter(
        p, calls, thread_id=thread, checkpointer=journal, step_boundaries=True
    )
    adapter.invoke(start=True)
    assert adapter.state().next == ("review",)
    first = journal.export()
    restored = LangGraphCheckpointJournal(thread, lambda *args: None, restored=first)
    resumed = LangGraphAdapter(
        p, calls, thread_id=thread, checkpointer=restored, step_boundaries=True
    )
    resumed.invoke()
    assert resumed.state().next == ("publish",)
    final = resumed.invoke()
    assert final["node_order"] == ["fetch", "review", "publish"]
    assert resumed.state().next == () and calls.position == 1
    assert {kind for _, kind, _ in saved} == {"checkpoint", "writes"}
    assert all(len(native) == 36 for _, _, native in saved)
    assert journal.export() == first


def test_untrusted_thread_and_native_note_boundary_rejected():
    p = program()
    with pytest.raises(AgentGitError):
        LangGraphAdapter(p, gateway(p), thread_id="user-selected-name")
    adapter = LangGraphAdapter(p, gateway(p), thread_id=str(uuid4()))
    with pytest.raises(AgentGitError):
        adapter.update_note("too-early")
    adapter.invoke(start=True)
    with pytest.raises(AgentGitError):
        adapter.update_note("x" * 2049)


def test_trace_provenance_is_checked_independently_of_payload():
    p = program()
    original = recording(p).outcome()

    class BadGateway:
        def call(self, **kwargs):
            return replace(
                original, trace=original.trace.model_copy(update={"signature_digest": "f" * 64})
            )

    adapter = LangGraphAdapter(p, BadGateway(), thread_id=str(uuid4()))
    assert adapter.invoke(start=True)["error_code"] == ErrorCode.REPLAY_MISMATCH.value


def native_run():
    p = program()
    calls = gateway(p)
    thread = str(uuid4())
    saved = []
    journal = LangGraphCheckpointJournal(
        thread, lambda raw, kind, native: saved.append((raw, kind, native))
    )
    adapter = LangGraphAdapter(p, calls, thread_id=thread, checkpointer=journal)
    adapter.invoke(start=True)
    return p, calls, thread, saved, journal, adapter


def test_pending_writes_recovery_skips_completed_fetch_even_before_next_checkpoint(monkeypatch):
    p, calls, thread, saved, _, _ = native_run()
    pending = next(
        raw
        for raw, kind, _ in saved
        if kind == "writes" and ["node_order", ["fetch"]] in decode(raw)["operations"][-1]["writes"]
    )
    monkeypatch.setattr("agentgit.adapters.mcp.MCPAdapter.call", forbidden)
    checkpoint = LangGraphCheckpointJournal(thread, lambda *args: None, restored=pending)
    recovered = LangGraphAdapter(p, calls, thread_id=thread, checkpointer=checkpoint)
    recovered.invoke()
    assert recovered.state().next == ("review",)
    values = recovered.invoke()
    assert values["node_order"] == ["fetch", "review", "publish"]
    assert values["report"].startswith("# 取证报告\n\ngraph-fixture-value")
    assert calls.position == 1


def test_native_fork_in_independent_thread_preserves_source_and_does_not_fetch(monkeypatch):
    p, calls, thread, _, journal, source = native_run()
    original = journal.export()
    snapshot = source.state()
    target_thread = str(uuid4())
    # 这里只核对 SDK 公共日志的线程语义；服务层仍须创建独立 Run/分支并保存来源。
    bundle = decode(original)
    bundle["thread_id"] = target_thread
    for operation in bundle["operations"]:
        operation["config"]["configurable"]["thread_id"] = target_thread
        if "metadata" in operation and "thread_id" in operation["metadata"]:
            operation["metadata"]["thread_id"] = target_thread
    fork_journal = LangGraphCheckpointJournal(
        target_thread, lambda *args: None, restored=encode(bundle)
    )
    monkeypatch.setattr("agentgit.adapters.mcp.MCPAdapter.call", forbidden)
    fork = LangGraphAdapter(p, calls, thread_id=target_thread, checkpointer=fork_journal)
    fork.update_note("独立分支补充说明")
    output = fork.invoke()
    assert "独立分支补充说明" in output["report"]
    assert "来源已核对" not in output["report"]
    assert output["node_order"] == ["fetch", "review", "publish"]
    assert journal.export() == original and source.state().values == snapshot.values
    assert source.state().next == ("review",) and calls.position == 1


def test_persist_failure_never_acknowledges_or_reuses_unconfirmed_index():
    p = program()
    thread = str(uuid4())
    durable = []

    def persist(raw, kind, native):
        durable.append(raw)
        raise RuntimeError("模拟提交后丢失回执")

    journal = LangGraphCheckpointJournal(thread, persist)
    original = journal.export()
    adapter = LangGraphAdapter(p, gateway(p), thread_id=thread, checkpointer=journal)
    with pytest.raises((RuntimeError, AgentGitError)):
        adapter.invoke(start=True)
    assert journal.export() == original and len(durable) == 1
    with pytest.raises(AgentGitError) as caught:
        adapter.state()
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    restored = LangGraphCheckpointJournal(thread, lambda *args: None, restored=durable[0])
    assert restored.get_tuple({"configurable": {"thread_id": thread}}) is not None


@pytest.mark.parametrize("operation", ["get", "list", "before", "put", "writes"])
def test_journal_scope_cannot_read_or_write_other_threads_or_namespaces(operation):
    _, _, thread, _, journal, adapter = native_run()
    current = journal.get_tuple({"configurable": {"thread_id": thread}})
    foreign = {"configurable": {"thread_id": str(uuid4()), "checkpoint_ns": ""}}
    with pytest.raises(AgentGitError) as caught:
        if operation == "get":
            journal.get_tuple(foreign)
        elif operation == "list":
            list(journal.list(foreign))
        elif operation == "before":
            list(journal.list(None, before=foreign))
        elif operation == "put":
            journal.put(foreign, current.checkpoint, current.metadata, {})
        else:
            journal.put_writes(foreign, [], str(uuid4()))
    assert caught.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(AgentGitError):
        journal.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": "subgraph"}})
    assert adapter.state().next == ("review",)


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "framework",
        "thread",
        "parent",
        "duplicate",
        "extra_config",
        "missing_fields",
        "writes_task",
        "writes_channel",
        "versions",
    ],
)
def test_corrupt_journal_cannot_be_hydrated(change):
    _, _, thread, _, journal, _ = native_run()
    bundle = decode(journal.export())
    operation = bundle["operations"][0]
    if change == "schema":
        bundle["schema_version"] = True
    elif change == "framework":
        bundle["framework_version"] = "different"
    elif change == "thread":
        bundle["thread_id"] = str(uuid4())
    elif change == "parent":
        operation["config"]["configurable"]["checkpoint_id"] = str(uuid4())
    elif change == "duplicate":
        bundle["operations"].append(operation)
    elif change == "extra_config":
        operation["config"]["secret"] = "must-not-load"
    elif change == "missing_fields":
        del operation["checkpoint"]
    elif change == "versions":
        operation["new_versions"] = {"__start__": "changed"}
    else:
        writes = next(item for item in bundle["operations"] if item["kind"] == "writes")
        if change == "writes_task":
            writes["task_id"] = "arbitrary"
        else:
            writes["writes"] = [[42, "invalid"]]
    with pytest.raises(AgentGitError):
        LangGraphCheckpointJournal(thread, forbidden, restored=encode(bundle))


def test_journal_operation_and_byte_limits_are_enforced_before_persist(monkeypatch):
    p = program()
    thread = str(uuid4())
    journal = LangGraphCheckpointJournal(thread, forbidden)
    monkeypatch.setattr("agentgit.runtime.langgraph_checkpoints.MAX_JOURNAL_OPERATIONS", 0)
    with pytest.raises(AgentGitError) as caught:
        LangGraphAdapter(p, gateway(p), thread_id=thread, checkpointer=journal).invoke(start=True)
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT
    monkeypatch.setattr("agentgit.runtime.langgraph_checkpoints.MAX_JOURNAL_BYTES", 0)
    with pytest.raises(AgentGitError) as caught:
        LangGraphCheckpointJournal(thread, forbidden)
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT


@pytest.mark.parametrize("stage", ["fetch", "review", "publish"])
def test_context_and_artifact_limits_produce_no_report(stage):
    base = program()
    changes = {"max_artifact_bytes": 65536} if stage == "publish" else {"max_context_bytes": 128}
    p = program(limits=BudgetLimits.model_validate({**base.limits.model_dump(), **changes}))
    if stage == "review":
        p = p.model_copy(update={"review_note": "备注" * 60})
    if stage == "publish":
        p = p.model_copy(update={"review_note": "x" * 1000})
    normalized = decode(recording(p).response)
    normalized["content"][0]["text"] = "x" * (
        65000 if stage == "publish" else 129 if stage == "fetch" else 80
    )
    adapter = LangGraphAdapter(p, gateway(p, normalized=normalized), thread_id=str(uuid4()))
    output = adapter.invoke(start=True)
    if adapter.state().next:
        output = adapter.invoke()
    assert output["error_code"] == ErrorCode.OUTPUT_LIMIT.value and output["report"] == ""


def test_program_arguments_are_copied_and_client_identity_is_checked_before_start():
    from agentgit.adapters.mcp import MCPClient

    p = program()
    adapter = LangGraphAdapter(p, gateway(p), thread_id=str(uuid4()))
    p.request.arguments["value"] = "changed-after-build"
    assert adapter.invoke(start=True)["fetch_text"] == "graph-fixture-value"
    foreign = MCPClient(MCPServer(server_identity="other", endpoint="http://127.0.0.1:1234/mcp"))
    with pytest.raises(AgentGitError) as caught:
        LangGraphAdapter(program(), gateway(program()), thread_id=str(uuid4()), client=foreign)
    assert caught.value.code == ErrorCode.CONFIGURATION


def test_malformed_configurable_is_an_explicit_scope_rejection():
    _, _, thread, _, journal, _ = native_run()
    raw = decode(journal.export())
    raw["operations"][0]["config"]["configurable"] = None
    with pytest.raises(AgentGitError) as caught:
        LangGraphCheckpointJournal(thread, forbidden, restored=encode(raw))
    assert caught.value.code == ErrorCode.FORBIDDEN


def test_clone_changes_only_scoped_thread_fields_and_preserves_uuid_text():
    source = str(uuid4())
    p = program().model_copy(update={"request": MCPArguments(arguments={"value": source})})
    calls = gateway(p)
    journal = LangGraphCheckpointJournal(source, lambda *args: None)
    graph = LangGraphAdapter(p, calls, thread_id=source, checkpointer=journal)
    graph.invoke(start=True)
    original = journal.export()
    target = str(uuid4())
    cloned = LangGraphCheckpointJournal(
        target, lambda *args: None, restored=journal.clone_review(target)
    )
    fork = LangGraphAdapter(p, calls, thread_id=target, checkpointer=cloned)
    assert fork.state().values["fetch_text"] == source
    assert fork.state().config["configurable"]["thread_id"] == target
    fork.update_note("独立备注")
    assert source in fork.invoke()["report"]
    assert journal.export() == original and graph.state().next == ("review",)
    assert [o.get("checkpoint", {}).get("id") for o in decode(original)["operations"]] == [
        o.get("checkpoint", {}).get("id")
        for o in decode(journal.clone_review(target))["operations"]
    ]


def test_clone_rejects_pending_writes_and_shared_thread():
    _, _, thread, saved, journal, _ = native_run()
    with pytest.raises(AgentGitError):
        journal.clone_review(thread)
    pending = next(
        raw
        for raw, kind, _ in saved
        if kind == "writes" and ["node_order", ["fetch"]] in decode(raw)["operations"][-1]["writes"]
    )
    incomplete = LangGraphCheckpointJournal(thread, lambda *args: None, restored=pending)
    with pytest.raises(AgentGitError):
        incomplete.clone_review(str(uuid4()))
