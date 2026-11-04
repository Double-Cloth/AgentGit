"""内存重算核心消费真实四角色录制；耐久新 Run 尚未接入。"""

import hashlib
import json
import os
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from test_collaboration_controls import command
from test_collaboration_execution import archive as archive
from test_collaboration_execution import drain, start
from test_collaboration_execution import flow as flow
from test_collaboration_recordings import counts

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.replay.collaboration import CollaborationRecordedEngine
from agentgit.services.collaboration_recordings import CollaborationRecordingReader
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def source(flow):
    return CollaborationRecordingReader(flow[0].history).inspect(
        flow[3], UUID(flow[4].collaboration_id), actor="collaboration-operator"
    )


def proof(request, value):
    destination = os.environ.get("AGENTGIT_COLLABORATION_REPLAY_PROOF_DIR")
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json"
        (root / name).write_text(
            json.dumps(dict(test=request.node.nodeid, **value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    "point",
    [
        None,
        "collaboration.call_reserved",
        "collaboration.after_saved_result",
        "collaboration.export_ready_saved",
    ],
)
def test_actual_recordings_recompute_every_checkpoint_and_outputs_without_clients(
    flow, point, monkeypatch, request
):
    queue = flow[0]
    start(flow)
    if point:
        Worker(queue).run_once()
        paused = []

        def pause(current):
            if current == point and not paused:
                command(flow, "pause", 0)
                paused.append(True)

        assert Worker(queue, fault=pause).run_once()["status"] == "paused"
        command(flow, "resume", 1)
    drain(queue)
    prepared = source(flow)
    before = counts(queue)

    def forbidden(*args, **kwargs):
        raise AssertionError("内存 Recorded 核心不能读取新字节或构造执行/网络/进程客户端")

    with monkeypatch.context() as isolated:
        isolated.setattr(queue.history.objects, "get", forbidden)
        for target in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
            isolated.setattr(target, "__init__", forbidden)
        isolated.setattr(socket, "socket", forbidden)
        isolated.setattr(socket, "create_connection", forbidden)
        isolated.setattr(subprocess, "Popen", forbidden)
        replay = CollaborationRecordedEngine(prepared).replay()
    assert replay.recorded_calls == 20
    assert replay.states == prepared.states
    assert replay.state_refs == tuple(
        tuple(f.state_ref for f in r.frames) for r in prepared.index.roles
    )
    assert dict(replay.artifacts).keys() == {"report.md", "sources.json"}
    assert before == counts(queue)
    proof(
        request,
        dict(
            source_index=prepared.index.model_dump(mode="json"),
            state_refs=replay.state_refs,
            recorded_calls=replay.recorded_calls,
            stop_boundaries=replay.stop_boundaries,
            actual_model_calls=0,
            actual_tool_calls=0,
            actual_network_requests=0,
            actual_subprocesses=0,
            before=before,
            after=counts(queue),
            durable_recorded_runs_created=False,
        ),
    )


@pytest.mark.parametrize(
    "fault", ["missing_call", "request", "response", "bytes", "causal_order", "false_checkpoint"]
)
def test_recorded_core_rejects_missing_or_changed_dependencies_without_live_fallback(
    flow, fault, request, monkeypatch
):
    start(flow)
    drain(flow[0])
    prepared = source(flow)
    if fault in ("missing_call", "request", "response"):
        recordings = list(prepared.recordings)
        first = recordings[0][0]
        if fault == "missing_call":
            recordings[0] = recordings[0][:-1]
        else:
            recordings[0] = (replace(first, **{fault: b"{}"}), *recordings[0][1:])
        prepared = replace(prepared, recordings=tuple(recordings))
    elif fault == "bytes":
        objects = list(prepared.objects)
        ref, kind, _ = objects[0]
        objects[0] = ref, kind, b"{}"
        prepared = replace(prepared, objects=tuple(objects))
    elif fault == "causal_order":
        events = list(prepared.events)
        events[0] = replace(events[0], order=events[1].order)
        prepared = replace(prepared, events=tuple(events))
    else:
        role = prepared.index.roles[0]
        frame = role.frames[0]
        raw = next(raw for ref, _, raw in prepared.objects if ref == frame.state_ref)
        snapshot = decode(raw)
        snapshot["data"]["usage"]["tokens"] += 1
        changed = encode(snapshot)
        ref = object_digest(ObjectType.STATE, changed)
        frames = (frame.model_copy(update={"state_ref": ref}), *role.frames[1:])
        roles = (role.model_copy(update={"frames": frames}), *prepared.index.roles[1:])
        index = prepared.index.model_copy(
            update=dict(
                roles=roles,
                object_refs=tuple(sorted((*prepared.index.object_refs, ref))),
                total_bytes=prepared.index.total_bytes + len(changed),
            )
        )
        prepared = replace(
            prepared, index=index, objects=(*prepared.objects, (ref, ObjectType.STATE, changed))
        )
    before = counts(flow[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("拒绝录制依赖时不能回退到执行客户端")

    with monkeypatch.context() as isolated:
        isolated.setattr(flow[0].history.objects, "get", forbidden)
        for target in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace, httpx.Client):
            isolated.setattr(target, "__init__", forbidden)
        isolated.setattr(socket, "socket", forbidden)
        isolated.setattr(socket, "create_connection", forbidden)
        isolated.setattr(subprocess, "Popen", forbidden)
        with pytest.raises(AgentGitError) as error:
            CollaborationRecordedEngine(prepared).replay()
    assert error.value.code in (
        ErrorCode.RECORDING_MISSING,
        ErrorCode.REPLAY_MISMATCH,
        ErrorCode.OBJECT_CORRUPT,
    )
    assert before == counts(flow[0])
    proof(
        request,
        dict(
            fault=fault,
            error=error.value.code.value,
            before=before,
            after=counts(flow[0]),
            durable_recorded_runs_created=False,
        ),
    )
