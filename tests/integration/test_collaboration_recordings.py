"""真实成功协作的只读来源核验；尚不代表重放执行器验收。"""

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from test_collaboration_controls import command
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, rows, start
from test_collaboration_execution import flow as flow

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.services import collaboration_recordings as module
from agentgit.services.collaboration_recordings import CollaborationRecordingReader
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def completed(flow):
    start(flow)
    drain(flow[0])
    return flow


def inspect(flow, **kwargs):
    queue, _, _, repo, receipt, _ = flow
    return CollaborationRecordingReader(queue.history).inspect(
        kwargs.get("repo", repo),
        kwargs.get("group", UUID(receipt.collaboration_id)),
        actor=kwargs.get("actor", "collaboration-operator"),
    )


def counts(queue):
    with queue.history.engine.connect() as c:
        return {
            name: c.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for name, table in TABLES.items()
        }


def proof(request, value):
    destination = os.environ.get("AGENTGIT_COLLABORATION_RECORDING_PROOF_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        import hashlib

        name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
        (path / name).write_text(
            json.dumps(dict(test=request.node.nodeid, **value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def test_success_source_is_complete_deterministic_read_only_and_constructs_no_clients(
    completed, monkeypatch, request
):
    queue, _, _, repo, _, _ = completed
    before, main = counts(queue), queue.history.branch(repo)

    def forbidden(*args, **kwargs):
        raise AssertionError("只读来源核验不得构造执行客户端或进程")

    for target in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
        monkeypatch.setattr(target, "__init__", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    source = inspect(completed)
    assert source == inspect(completed)
    assert [len(r) for r in source.recordings] == [2, 8, 7, 3]
    assert len(source.index.message_ids) == 7 and len(source.index.shared_head_refs) == 5
    assert sum(len(r) for r in source.states) > 60
    assert before == counts(queue) and main == queue.history.branch(repo)
    assert source.index.total_bytes == sum(
        len(
            queue.history.objects.get(
                ref,
                ObjectType(
                    next(o["object_type"] for o in rows(queue, "objects") if o["id"] == ref)
                ),
            )
        )
        for ref in source.index.object_refs
    )
    proof(
        request,
        dict(
            index=source.index.model_dump(mode="json"),
            before=before,
            after=counts(queue),
            clients_constructed=0,
            replay_executed=False,
        ),
    )


@pytest.mark.parametrize(
    "point",
    [
        "collaboration.call_reserved",
        "collaboration.after_saved_result",
        "collaboration.export_ready_saved",
    ],
)
def test_success_source_includes_group_pause_resume_and_unstarted_or_saved_call(
    flow, point, request
):
    queue = flow[0]
    start(flow)
    Worker(queue).run_once()
    stopped = []

    def pause(current):
        if current == point and not stopped:
            command(flow, "pause", 0)
            stopped.append(True)

    assert Worker(queue, fault=pause).run_once()["status"] == "paused"
    command(flow, "resume", 1)
    drain(queue)
    source = inspect(flow)
    events = [
        e for e in rows(queue, "events") if e["type"] == "collaboration.group_control_requested"
    ]
    assert len(events) == 2 and sum(len(c) for c in source.recordings) == 20
    assert all(e["payload_ref"] in source.index.object_refs for e in events)
    proof(
        request,
        dict(
            index=source.index.model_dump(mode="json"),
            pause_point=point,
            group_control_count=len(events),
            replay_executed=False,
        ),
    )


@pytest.mark.parametrize("identity", ["actor", "repo", "group"])
def test_unowned_source_rejected_before_any_cas_read(completed, identity, monkeypatch):
    def forbidden(*args):
        raise AssertionError("身份拒绝必须先于私有字节读取")

    monkeypatch.setattr(completed[0].history.objects, "get", forbidden)
    value = "another-operator" if identity == "actor" else uuid4()
    with pytest.raises(AgentGitError) as error:
        inspect(completed, **{identity: value})
    assert error.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize("guard", ["source_revision", "dependency_lock_digest"])
def test_current_source_and_lock_guards_are_not_relaxed(completed, guard, monkeypatch):
    monkeypatch.setattr(module, guard, lambda: "0" * 64)
    with pytest.raises(AgentGitError) as error:
        inspect(completed)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


@pytest.mark.parametrize("source", ["prepared", "cancelled"])
def test_incomplete_or_cancelled_group_is_not_success_recording(flow, source):
    if source == "cancelled":
        command(flow, "cancel", 0)
        drain(flow[0])
    with pytest.raises(AgentGitError) as error:
        inspect(flow)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


@pytest.mark.parametrize("kind", ["request", "response", "state", "envelope", "head"])
def test_missing_physical_source_bytes_fail_without_any_new_history(completed, kind, request):
    queue = completed[0]
    call = rows(queue, "external_calls")[0]
    if kind == "request":
        ref = call["request_digest"]
    elif kind == "response":
        ref = queue.history.objects.get_json(call["result_ref"], ObjectType.CALL)["response_ref"]
    elif kind == "state":
        ref = rows(queue, "snapshots")[0]["state_ref"]
    elif kind == "envelope":
        ref = rows(queue, "collaboration_messages")[0]["envelope_ref"]
    else:
        ref = rows(queue, "collaboration_publications")[0]["head_ref"]
    path = queue.history.objects.path_for(ref)
    raw, before = path.read_bytes(), counts(queue)
    path.unlink()
    try:
        with pytest.raises(AgentGitError) as error:
            inspect(completed)
        assert error.value.code == ErrorCode.NOT_FOUND
        assert counts(queue) == before
        proof(
            request,
            dict(
                missing_kind=kind,
                missing_ref=ref,
                error=error.value.code.value,
                history_unchanged=True,
                replay_executed=False,
            ),
        )
    finally:
        path.write_bytes(raw)


@pytest.mark.parametrize(
    "table,field",
    [
        ("collaboration_receipts", "received_seq"),
        ("collaboration_messages", "sent_seq"),
        ("collaboration_publications", "event_seq"),
        ("run_commits", "run_order"),
        ("external_calls", "request_digest"),
    ],
)
def test_inconsistent_source_index_is_rejected(completed, table, field, monkeypatch, request):
    original = module._SourceReader.rows

    def changed(self, name, *conditions, **kwargs):
        result = original(self, name, *conditions, **kwargs)
        if name == table and result:
            result[0][field] = "0" * 64 if field == "request_digest" else 9999
        return result

    monkeypatch.setattr(module._SourceReader, "rows", changed)
    with pytest.raises(AgentGitError) as error:
        inspect(completed)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
    proof(
        request,
        dict(
            test_only_reader_index_fault=dict(table=table, field=field),
            error=error.value.code.value,
            replay_executed=False,
        ),
    )
