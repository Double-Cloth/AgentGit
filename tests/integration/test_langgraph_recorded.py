"""固定图真实范围回放：消费调用及控制，保持来源且禁止外部客户端。"""

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_langgraph_forks import fingerprint, fork_request, ready
from test_langgraph_runs import archive as archive
from test_langgraph_runs import case, control, forbidden, state
from test_mcp_adapter import server as server
from test_merge_process_crash import kill_interpreter
from typer.testing import CliRunner

from agentgit.cli.main import app
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph_recorded import NewLangGraphRecorded
from agentgit.core.langgraph_runs import LangGraphRunState
from agentgit.core.models import RunStatus, parse_contract
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.langgraph_recordings import prepare_recording
from agentgit.services.management import ManagementService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def checkpoints(history, repo, run):
    with history.engine.connect() as connection:
        rows = list(
            connection.execute(
                sa.select(TABLES["run_commits"])
                .where(TABLES["run_commits"].c.run_id == run)
                .order_by(TABLES["run_commits"].c.run_order)
            ).mappings()
        )
    return [
        (
            row["commit_id"],
            parse_contract(encode(history.restore(repo, row["commit_id"]).data), LangGraphRunState),
        )
        for row in rows
    ]


def request(run, first, last, name="recorded"):
    return NewLangGraphRecorded(
        source_run_id=str(run),
        from_commit=first,
        to_commit=last,
        name=name,
        expected_version=0,
    )


def finished(history, server, tmp_path, *, pause=True, queued_pause=False):
    queue, service, repo, program, receipt, counts, _ = case(history, server, tmp_path, pause=pause)
    run = UUID(receipt.run_id)
    if queued_pause:
        control(queue, repo, run, "pause")
        assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "paused"
        control(queue, repo, run, "resume")
    result = Worker(queue).run_once()
    if pause:
        assert result["status"] == "paused", result
        control(queue, repo, run, "resume")
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    final, _, row = state(history, repo, run)
    return queue, service, repo, run, final, row["checkpoint_commit_id"], counts


@pytest.mark.parametrize("start", ["cold", "prepared", "writes", "review", "completed"])
def test_full_graph_recorded_consumes_native_control_and_matches_report(
    history, server, tmp_path, start, monkeypatch
):
    queue, service, repo, source, final, last, counts = finished(history, server, tmp_path)
    path = checkpoints(history, repo, source)
    first = {
        "cold": lambda s: s.native_ref is None,
        "prepared": lambda s: s.pending,
        "writes": lambda s: s.native_operation == "writes" and s.trace is not None,
        "review": lambda s: s.status == RunStatus.PAUSED,
        "completed": lambda s: s.status == RunStatus.COMPLETED,
    }[start]
    from_commit = next(cid for cid, candidate in path if first(candidate))
    source_bytes, before_counts = fingerprint(history, queue, repo, source), counts()
    spec = request(source, from_commit, last)
    receipt = service.recorded(repo, spec, actor="graph-operator", key="once")
    assert service.recorded(repo, spec, actor="graph-operator", key="once") == receipt
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-invalid-key")
    attempts = []

    def no_connect(*args, **kwargs):
        attempts.append(str(args[1:]))
        raise AssertionError("Recorded 不能连接网络或追踪端点")

    with monkeypatch.context() as execution:
        execution.setattr(socket.socket, "connect", no_connect)
        execution.setattr(subprocess, "Popen", forbidden)
        result = Worker(queue, mcp_client_factory=forbidden, model_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    replay, snapshot, row = state(history, repo, UUID(receipt.run_id))
    assert replay.logical_projection() == final.logical_projection()
    assert replay.trace == final.trace and replay.actual_attempts == 0
    assert row["mode"] == "recorded" and row["origin_run_id"] == source
    assert fingerprint(history, queue, repo, source) == source_bytes
    assert counts() == before_counts
    assert attempts == []
    (tmp_path / "recorded-no-effects.json").write_text(
        json.dumps(
            {
                "source_revision": source_revision(),
                "run_id": receipt.run_id,
                "socket_attempts": attempts,
                "before_counts": before_counts,
                "after_counts": counts(),
                "logical_projection": replay.logical_projection(),
                "source_projection": final.logical_projection(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["external_calls"])
                .where(TABLES["external_calls"].c.run_id == UUID(receipt.run_id))
            ).scalar_one()
            == 0
        )
    view = ManagementService(queue.research, actor="graph-operator").run(repo, UUID(receipt.run_id))
    assert view["adapter"]["recording"]["from_commit"] == from_commit
    assert view["adapter"]["actual_attempts"] == 0
    assert {a.path for a in snapshot.artifacts} == {"langgraph-native.json", "report.md"}


@pytest.mark.parametrize("pause", [False, True])
def test_recorded_consumes_queued_and_native_controls_and_can_replay_itself(
    history, server, tmp_path, pause
):
    queue, service, repo, source, final, last, counts = finished(
        history, server, tmp_path, pause=pause, queued_pause=True
    )
    first = checkpoints(history, repo, source)[0][0]
    prepared = prepare_recording(history, repo, source, first, last)
    assert [c.kind for c in prepared.bundle.controls] == (
        ["requested", "native"] if pause else ["requested"]
    )
    receipt = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="first"
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    run = UUID(receipt.run_id)
    recorded, _, row = state(history, repo, run)
    second = service.recorded(
        repo,
        request(run, first, row["checkpoint_commit_id"], "second"),
        actor="graph-operator",
        key="second",
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    assert (
        state(history, repo, UUID(second.run_id))[0].logical_projection()
        == final.logical_projection()
    )
    assert recorded.replay_control_index == len(prepared.bundle.controls)
    assert counts()["calls"] == 1


@pytest.mark.parametrize("start", ["base", "seed", "updated"])
def test_fork_recorded_reapplies_only_pure_native_note(history, server, tmp_path, start):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    fork = service.fork(repo, fork_request(source, checkpoint), actor="graph-operator", key="fork")
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    run = UUID(fork.run_id)
    path = checkpoints(history, repo, run)
    first = (
        checkpoint
        if start == "base"
        else path[0][0]
        if start == "seed"
        else next(cid for cid, s in path if s.fork_initialized)
    )
    final, _, row = state(history, repo, run)
    before = fingerprint(history, queue, repo, run)
    replay = service.recorded(
        repo,
        request(run, first, row["checkpoint_commit_id"]),
        actor="graph-operator",
        key="recorded",
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    assert (
        state(history, repo, UUID(replay.run_id))[0].logical_projection()
        == final.logical_projection()
    )
    assert fingerprint(history, queue, repo, run) == before and counts()["calls"] == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_current_recorded_control_remains_independent(history, server, tmp_path, action):
    queue, service, repo, source, final, last, counts = finished(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    replay = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="recorded"
    )
    run = UUID(replay.run_id)
    control(queue, repo, run, action)
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == ("paused" if action == "pause" else "cancelled"), result
    assert counts()["calls"] == 1
    if action == "pause":
        control(queue, repo, run, "resume")
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
        assert result["status"] == "completed", result
        assert state(history, repo, run)[0].logical_projection() == final.logical_projection()


@pytest.mark.parametrize("damage", ["result", "resume", "program", "mode"])
def test_missing_source_or_wrong_mode_fails_without_live_fallback(
    history, server, tmp_path, damage, monkeypatch
):
    queue, service, repo, source, _, last, counts = finished(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    replay = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="recorded"
    )
    run = UUID(replay.run_id)
    missing = None
    with history.engine.begin() as connection:
        if damage == "result":
            connection.execute(
                TABLES["external_calls"]
                .update()
                .where(TABLES["external_calls"].c.run_id == source)
                .values(result_ref=None)
            )
        elif damage == "resume":
            missing = connection.execute(
                sa.select(TABLES["events"].c.payload_ref).where(
                    TABLES["events"].c.run_id == source,
                    TABLES["events"].c.type == "control.requested",
                )
            ).scalar_one()
        elif damage == "mode":
            connection.execute(
                TABLES["runs"].update().where(TABLES["runs"].c.id == run).values(mode="live")
            )
        else:
            missing = service.payload(repo, UUID(replay.job_id)).program_ref
    original = history.objects.get

    def missing_read(reference, *args, **kwargs):
        if reference == missing:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "测试注入：固定来源暂不可读")
        return original(reference, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(history.objects, "get", missing_read)
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "failed", result
    assert counts()["calls"] == 1
    (tmp_path / "recorded-rejection.json").write_text(
        json.dumps(
            {
                "damage": damage,
                "missing_ref": missing,
                "run_id": replay.run_id,
                "result": result,
                "counts": counts(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_incomplete_source_is_rejected_before_branch_publication(history, server, tmp_path):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    with pytest.raises(AgentGitError) as caught:
        service.recorded(
            repo, request(source, first, checkpoint), actor="graph-operator", key="incomplete"
        )
    assert caught.value.code == ErrorCode.RECORDING_MISSING
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branches"])
            ).scalar_one()
            == 1
        )
    assert counts()["calls"] == 1


def test_recorded_cli_and_safe_fork_of_recorded_preserve_source(
    history, server, tmp_path, monkeypatch
):
    queue, service, repo, source, _, last, counts = finished(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    management = ManagementService(queue.research, actor="graph-operator")
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: management)
    path = tmp_path / "graph-recorded.json"
    path.write_bytes(encode(request(source, first, last).model_dump(mode="json")))
    runner = CliRunner()
    args = ["langgraph-recorded", str(repo), str(path), "--key", "cli-recorded", "--testing"]
    response = runner.invoke(app, args)
    assert response.exit_code == 0, response.output
    receipt = json.loads(response.output)
    assert json.loads(runner.invoke(app, args).output) == receipt
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    run = UUID(receipt["run_id"])
    safe = next(
        cid
        for cid, s in checkpoints(history, repo, run)
        if s.native_operation == "checkpoint" and s.native_next == ("review",)
    )
    before = fingerprint(history, queue, repo, run)
    fork = service.fork(
        repo,
        fork_request(run, safe, "replayed-fork", "回放后分叉"),
        actor="graph-operator",
        key="fork-recorded",
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    assert "回放后分叉" in state(history, repo, UUID(fork.run_id))[0].values["report"]
    assert fingerprint(history, queue, repo, run) == before and counts()["calls"] == 1


def test_initial_paused_range_can_itself_be_recorded(history, server, tmp_path):
    queue, service, repo, source, final, last, counts = finished(history, server, tmp_path)
    first = next(
        cid for cid, s in checkpoints(history, repo, source) if s.status == RunStatus.PAUSED
    )
    previous = source
    for index in range(2):
        receipt = service.recorded(
            repo,
            request(previous, first, last, "paused-" + str(index)),
            actor="graph-operator",
            key="paused-" + str(index),
        )
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
        assert result["status"] == "completed", result
        previous = UUID(receipt.run_id)
        current, _, row = state(history, repo, previous)
        assert current.logical_projection() == final.logical_projection()
        last = row["checkpoint_commit_id"]
        first = checkpoints(history, repo, previous)[0][0]
    assert counts()["calls"] == 1


@pytest.mark.parametrize(
    "field", ["controls", "source", "result", "program", "environment", "usage"]
)
def test_forged_recording_bundle_is_not_trusted_as_source(history, server, tmp_path, field):
    queue, service, repo, source, _, last, counts = finished(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    receipt = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="forged"
    )
    job = queue.get(repo, UUID(receipt.job_id))
    payload = history.objects.get_json(job["payload_ref"], ObjectType.BLOB)
    bundle = history.objects.get_json(payload["replay"]["bundle_ref"], ObjectType.BLOB)
    if field == "controls":
        bundle["controls"] = []
    elif field == "source":
        bundle["source_run_id"] = receipt.run_id
    elif field == "result":
        bundle["result_ref"] = "0" * 64
    elif field == "program":
        bundle["program_ref"] = "0" * 64
    elif field == "environment":
        bundle["environment_ref"] = "0" * 64
    else:
        bundle["needs_fetch"] = False
    payload["replay"]["bundle_ref"] = history.register_input(repo, encode(bundle))
    changed = history.objects.put_json(ObjectType.BLOB, payload)
    with history.engine.begin() as connection:
        history._ref(connection, repo, changed, "job", receipt.job_id, "payload")
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(receipt.job_id))
            .values(payload_ref=changed.id)
        )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "failed" and result["error_code"] == "replay_mismatch", result
    assert counts()["calls"] == 1


def test_recorded_wall_clock_is_not_replaced_by_source_time(history, server, tmp_path):
    queue, service, repo, source, _, last, counts = finished(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    receipt = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="expired"
    )

    def expire(point):
        if point == "langgraph.after_recorded_seed":
            with history.engine.begin() as connection:
                connection.execute(
                    TABLES["jobs"]
                    .update()
                    .where(TABLES["jobs"].c.id == UUID(receipt.job_id))
                    .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
                )

    result = Worker(queue, fault=expire, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "budget_exhausted", result
    assert state(history, repo, UUID(receipt.run_id))[0].actual_attempts == 0
    assert counts()["calls"] == 1


@pytest.mark.parametrize(
    "point",
    [
        "langgraph.after_recorded_seed",
        "langgraph.after_recorded_call",
        "langgraph.after_fetch_writes",
        "langgraph.after_fetch_checkpoint",
        "langgraph.after_recorded_pause",
    ],
)
def test_actual_recorded_worker_death_recovers_without_any_external_calls(
    history, server, tmp_path, point
):
    queue, service, repo, source, final, last, counts = finished(history, server, tmp_path)
    first = checkpoints(history, repo, source)[0][0]
    before_source = fingerprint(history, queue, repo, source)
    receipt = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="process-recorded"
    )
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "recorded-boundary.json"
    env = os.environ.copy()
    env.update(
        AGENTGIT_GRAPH_TEST_SCHEMA=schema,
        AGENTGIT_GRAPH_TEST_POINT=point,
        AGENTGIT_GRAPH_TEST_DATABASE=history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_GRAPH_TEST_OBJECTS=str(history.objects.root),
        AGENTGIT_GRAPH_TEST_DATA=str(tmp_path / "data"),
        AGENTGIT_GRAPH_TEST_MARKER=str(marker),
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).parents[1] / "helpers/langgraph_worker_process.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = monotonic() + 25
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode("utf-8", errors="replace"))
            assert monotonic() < deadline, "Recorded Worker 未抵达确切边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text("utf-8"))
        assert (
            boundary["source_revision"] == source_revision()
            and boundary["lock_digest"] == dependency_lock_digest()
        )
        assert (
            boundary["run_id"] == receipt.run_id
            and boundary["schema"] == schema
            and boundary["point"] == point
        )
        assert boundary["pid"] == process.pid or boundary["parent_pid"] == process.pid
        assert process.poll() is None
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.terminate()
        process.wait(timeout=10)
        if os.name == "nt":
            assert process.returncode == 73
        before_counts = counts()
        with history.engine.begin() as connection:
            old = connection.execute(
                sa.select(TABLES["jobs"].c.fencing_token).where(
                    TABLES["jobs"].c.id == UUID(receipt.job_id)
                )
            ).scalar_one()
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == UUID(receipt.job_id))
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
        assert result["status"] == "completed", result
        current, _, _ = state(history, repo, UUID(receipt.run_id))
        assert current.actual_attempts == 0
        assert current.logical_projection() == final.logical_projection()
        assert counts() == before_counts and counts()["calls"] == 1
        assert fingerprint(history, queue, repo, source) == before_source
        with history.engine.connect() as connection:
            new = connection.execute(
                sa.select(TABLES["jobs"].c.fencing_token).where(
                    TABLES["jobs"].c.id == UUID(receipt.job_id)
                )
            ).scalar_one()
        assert new > old
        (tmp_path / "recorded-process-evidence.json").write_text(
            json.dumps(
                {
                    "boundary": boundary,
                    "launcher_pid": process.pid,
                    "exit_code": process.returncode,
                    "before_fence": old,
                    "after_fence": new,
                    "before_counts": before_counts,
                    "after_counts": counts(),
                    "source_before_sha256": hashlib.sha256(before_source).hexdigest(),
                    "source_after_sha256": hashlib.sha256(
                        fingerprint(history, queue, repo, source)
                    ).hexdigest(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stderr.close()
