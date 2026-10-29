"""公共 checkpoint 日志连接真实 History/CAS/MCP；不代替尚待接入的 Job/Worker。"""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_mcp_adapter import server as server

from agentgit import __version__
from agentgit.adapters.langgraph import LangGraphAdapter
from agentgit.adapters.mcp import MCPClient
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import LangGraphProgram
from agentgit.core.mcp import MCPArguments
from agentgit.core.models import EffectClass, EnvironmentManifest
from agentgit.replay.recorded import RecordedAdapter
from agentgit.runtime.langgraph_checkpoints import LangGraphCheckpointJournal
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput, HistoryService
from agentgit.storage.objects import ObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def archive(history, server, request):
    yield
    destination = os.environ.get("AGENTGIT_LANGGRAPH_EVIDENCE_DIR")
    if destination is None:
        return
    _, counts, _ = server
    with history.engine.connect() as connection:
        tables = {
            name: [dict(row) for row in connection.execute(sa.select(TABLES[name])).mappings()]
            for name in (
                "objects",
                "object_refs",
                "runs",
                "branches",
                "commits",
                "snapshots",
                "events",
                "external_calls",
            )
        }
    objects = []
    for row in tables["objects"]:
        payload = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            {
                "id": row["id"],
                "type": row["object_type"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    packet = {
        "kind": "langgraph_native_history_fixture/v1",
        "test": request.node.nodeid,
        "source_revision": source_revision(),
        "lock_digest": dependency_lock_digest(),
        "tables": tables,
        "objects": objects,
        "counts": counts(),
    }
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16] + ".json"
    with (folder / name).open("x", encoding="utf-8") as stream:
        json.dump(packet, stream, default=str, ensure_ascii=False, indent=2)


def setup(history, server):
    config, counts, _ = server
    # 沿用工具 fixture；本组只核对成功路径，显式为启动/发现留出有界期限。
    config = config.model_copy(update={"timeout_ms": 2000})
    client = MCPClient(config)
    binding = client.bind("echo", EffectClass.READ_ONLY)
    p = LangGraphProgram(
        binding=binding,
        request=MCPArguments(arguments={"value": "graph-fixture-value"}),
        review_note="来源已核对",
    )
    repository = history.create_repository("LangGraph 原生 History 工程 fixture")
    run = history.create_run(repository)
    program_ref = history.register_input(repository, encode(p.model_dump(mode="json")))
    environment = EnvironmentManifest(
        runtime_version=__version__,
        python_version="3.12",
        code_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        model_signature="no-model/langgraph-native-fixture/v1",
        prompt_digest=object_digest(ObjectType.BLOB, b"langgraph-native-fixture/v1"),
        input_ref=program_ref,
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )
    cursor = {"head": None, "version": 0}

    def persistence(service, selected):
        def persist(raw, kind, native):
            bundle = decode(raw)
            last = bundle["operations"][-1]
            values = last.get("checkpoint", {}).get("channel_values", {})
            artifacts = (
                (ArtifactInput("report.md", values["report"].encode(), "text/markdown"),)
                if values.get("report")
                else ()
            )
            result = service.commit(
                repository,
                run,
                expected_head=selected["head"],
                expected_version=selected["version"],
                state={
                    "kind": "langgraph_native_fixture/v1",
                    "program_ref": program_ref,
                    "journal": bundle,
                },
                environment=environment,
                events=(
                    EventInput(
                        "langgraph.native." + kind,
                        "graph",
                        {
                            "native_checkpoint_id": native,
                            "journal_sha256": hashlib.sha256(raw).hexdigest(),
                        },
                    ),
                ),
                artifacts=artifacts,
            )
            selected.update(head=result.commit_id, version=result.branch_version)

        return persist

    authorization = ApprovalService(
        history, ToolPolicy(rules=(binding.rule(),)), actor="graph-operator"
    )
    gateway = RecordedCallGateway(
        history,
        repository,
        run,
        lambda: (cursor["head"], cursor["version"]),
        authorization=authorization,
    )
    journal = LangGraphCheckpointJournal(str(run), persistence(history, cursor))
    adapter = LangGraphAdapter(p, gateway, thread_id=str(run), client=client, checkpointer=journal)
    return repository, run, p, cursor, persistence, journal, adapter, counts


def forbidden(*args, **kwargs):
    raise AssertionError("恢复纯节点不应重新获取工具或创建客户端")


def test_actual_mcp_pause_reopen_history_and_native_resume_publish(history, server, monkeypatch):
    repository, run, p, cursor, persistence, journal, adapter, counts = setup(history, server)
    paused = adapter.invoke(start=True)
    assert paused["node_order"] == ["fetch"] and adapter.state().next == ("review",)
    raw = journal.export()
    old_head = cursor["head"]
    restarted = HistoryService(history.engine, ObjectStore(history.objects.root))
    snapshot = restarted.restore(repository, old_head)
    assert encode(snapshot.data["journal"]) == raw
    before = counts()
    monkeypatch.setattr("agentgit.adapters.mcp.MCPAdapter.call", forbidden)
    saved = LangGraphCheckpointJournal(str(run), persistence(restarted, cursor), restored=raw)
    resumed = LangGraphAdapter(
        p, RecordedAdapter((), lambda: None), thread_id=str(run), checkpointer=saved
    )
    final = resumed.invoke()
    assert resumed.state().next == () and final["node_order"] == ["fetch", "review", "publish"]
    artifact = restarted.artifacts(repository, cursor["head"])[0]
    assert restarted.read_artifact(repository, artifact["id"]) == final["report"].encode()
    assert counts() == before and before["calls"] == 1
    assert restarted.restore(repository, old_head).data == snapshot.data
    events = restarted.events(repository, run)
    native = [row for row in events if row["type"].startswith("langgraph.native.")]
    assert len(native) == len(decode(saved.export())["operations"])
    assert {row["type"] for row in native} == {
        "langgraph.native.checkpoint",
        "langgraph.native.writes",
    }


def test_stale_head_native_save_fails_before_ack_and_preserves_current_history(history, server):
    repository, run, p, cursor, persistence, journal, adapter, counts = setup(history, server)
    adapter.invoke(start=True)
    raw = journal.export()
    stale = dict(cursor)
    adapter.invoke()
    current = dict(cursor)
    before = counts()
    loser = LangGraphCheckpointJournal(str(run), persistence(history, stale), restored=raw)
    resumed = LangGraphAdapter(
        p, RecordedAdapter((), lambda: None), thread_id=str(run), checkpointer=loser
    )
    with pytest.raises(AgentGitError) as caught:
        resumed.invoke()
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert loser.export() == raw and history.branch(repository)["head_commit_id"] == current["head"]
    assert counts() == before


def test_native_state_and_journal_remain_project_scoped(history, server):
    repository, run, _, cursor, _, journal, adapter, counts = setup(history, server)
    adapter.invoke(start=True)
    other = history.create_repository("其他项目")
    before = counts()
    with pytest.raises(AgentGitError) as caught:
        history.restore(other, cursor["head"])
    assert caught.value.code == ErrorCode.NOT_FOUND
    assert encode(history.restore(repository, cursor["head"]).data["journal"]) == journal.export()
    assert counts() == before and str(run) == decode(journal.export())["thread_id"]
