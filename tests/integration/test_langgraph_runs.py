"""固定图真实 Job/Worker、暂停、用量与原始引用；完整 Recorded 另行核对。"""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_mcp_adapter import server as server
from test_merge_process_crash import kill_interpreter

from agentgit.adapters.mcp import MCPClient
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import LangGraphDefinition, LangGraphProgram
from agentgit.core.langgraph_runs import LangGraphRunState
from agentgit.core.mcp import MCPArguments
from agentgit.core.models import EffectClass, parse_contract
from agentgit.core.research import BudgetLimits
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.langgraph_runs import LangGraphRunService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

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
                "repositories",
                "runs",
                "jobs",
                "branches",
                "commits",
                "snapshots",
                "artifacts",
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
        "kind": "langgraph_worker_raw/v1",
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


def case(history, server, tmp_path, *, pause=True, submit=True):
    config, counts, mode = server
    config = config.model_copy(update={"timeout_ms": 2000})
    binding = MCPClient(config).bind("echo", EffectClass.READ_ONLY)
    p = LangGraphProgram(
        binding=binding,
        request=MCPArguments(arguments={"value": "graph-fixture-value"}),
        review_note="来源已核对",
        graph=LangGraphDefinition(pause_before=("review",) if pause else ()),
    )
    queue = JobService(ResearchService(history, tmp_path / "data"), lease_ms=1000)
    service = LangGraphRunService(queue)
    repo = history.create_repository("LangGraph 固定图真实 Worker")
    submitted = (
        service.submit(
            repo,
            p,
            actor="graph-operator",
            key="graph-once",
            branch_name="main",
            expected_head=None,
            expected_version=0,
        )
        if submit
        else None
    )
    return queue, service, repo, p, submitted, counts, mode


def state(history, repo, run):
    with history.engine.connect() as connection:
        row = dict(history._run(connection, repo, run))
    snapshot = history.restore(repo, row["checkpoint_commit_id"])
    return parse_contract(encode(snapshot.data), LangGraphRunState), snapshot, row


def control(queue, repo, run, action):
    return queue.request_control(
        repo, run, action, expected_version=queue.run(repo, run)["state_version"]
    )


def forbidden(*args, **kwargs):
    pytest.fail("当前恢复/控制路径不能创建真实客户端或调用模型")


def test_worker_native_pause_resume_and_artifacts_with_one_actual_fetch(history, server, tmp_path):
    queue, service, repo, p, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    first = Worker(queue, model_factory=forbidden).run_once()
    assert first["status"] == "paused", first
    paused, _, row = state(history, repo, run)
    assert paused.native_next == ("review",) and paused.values["node_order"] == ["fetch"]
    assert paused.review_boundary_reached and paused.usage.steps == 1
    assert paused.usage.tool_calls == 1 and paused.usage.model_calls == 0
    assert counts()["calls"] == 1
    before = counts()
    control(queue, repo, run, "resume")
    final = Worker(queue, mcp_client_factory=forbidden, model_factory=forbidden).run_once()
    assert final["status"] == "completed", final
    completed, snapshot, row = state(history, repo, run)
    assert completed.usage.steps == 3 and completed.actual_attempts == 1
    assert (
        completed.values["node_order"] == ["fetch", "review", "publish"]
        and completed.native_next == ()
    )
    assert {a.path for a in snapshot.artifacts} == {"langgraph-native.json", "report.md"}
    report = next(a for a in snapshot.artifacts if a.path == "report.md")
    assert (
        history.objects.get(report.object_ref, ObjectType.BLOB)
        == completed.values["report"].encode()
    )
    assert counts() == before and row["control_status"] == "applied"
    assert Worker(queue).run_once() is None
    assert (
        service.submit(
            repo,
            p,
            actor="graph-operator",
            key="graph-once",
            branch_name="main",
            expected_head=None,
            expected_version=0,
        )
        == submitted
    )


def test_graph_without_user_breakpoint_finishes_in_one_claim(history, server, tmp_path):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path, pause=False)
    result = Worker(queue).run_once()
    assert result["status"] == "completed", result
    current, _, _ = state(history, repo, UUID(submitted.run_id))
    assert current.usage.steps == 3 and not current.review_boundary_reached
    assert counts()["calls"] == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_queued_control_has_zero_tool_requests(history, server, tmp_path, action):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    control(queue, repo, run, action)
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == ("paused" if action == "pause" else "cancelled"), result
    assert counts()["calls"] == 0
    current, _, _ = state(history, repo, run)
    assert current.usage.steps == 0 and current.usage.tool_calls == 0
    if action == "pause":
        control(queue, repo, run, "resume")
        assert Worker(queue).run_once()["status"] == "paused"
        control(queue, repo, run, "resume")
        assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
        assert counts()["calls"] == 1


@pytest.mark.parametrize("mode", ["tool_error", "drop"])
def test_known_readonly_failure_is_terminal_and_never_reissues(history, server, tmp_path, mode):
    queue, _, repo, _, submitted, counts, set_mode = case(history, server, tmp_path)
    set_mode(mode)
    result = Worker(queue).run_once()
    assert result["status"] == "failed", result
    current, snapshot, _ = state(history, repo, UUID(submitted.run_id))
    assert current.error_code == "tool_error" and current.values["node_order"] == ["fetch"]
    assert current.usage.tool_calls == 1 and counts()["calls"] == 1
    assert not any(a.path == "report.md" for a in snapshot.artifacts)
    assert Worker(queue).run_once() is None


def test_expired_resume_cannot_create_client_or_increase_usage(history, server, tmp_path):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    control(queue, repo, run, "pause")
    assert Worker(queue).run_once()["status"] == "paused"
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
            .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    control(queue, repo, run, "resume")
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "budget_exhausted"
    current, _, _ = state(history, repo, run)
    assert current.usage.steps == 0 and counts()["calls"] == 0


def test_submission_identity_writer_and_cross_project_payload_boundaries(history, server, tmp_path):
    queue, service, repo, p, submitted, counts, _ = case(history, server, tmp_path)
    for key in ["graph-once", "different"]:
        changed = p.model_copy(update={"review_note": "different"}) if key == "graph-once" else p
        with pytest.raises(AgentGitError) as caught:
            service.submit(
                repo,
                changed,
                actor="graph-operator",
                key=key,
                branch_name="main",
                expected_head=None,
                expected_version=0,
            )
        assert caught.value.code == ErrorCode.VERSION_CONFLICT
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError):
        service.payload(other, UUID(submitted.job_id))
    assert counts()["calls"] == 0


def test_current_worker_rejects_forged_code_identity_before_graph(
    history, server, tmp_path, monkeypatch
):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    monkeypatch.setattr(
        "agentgit.runtime.langgraph_run.source_revision", lambda: "source-sha256:" + "0" * 64
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "failed" and result["error_code"] == "unsupported_schema", result
    assert (
        counts()["calls"] == 0
        and queue.run(repo, UUID(submitted.run_id))["checkpoint_commit_id"] is None
    )


def test_native_artifact_budget_error_is_preserved_and_does_not_become_lease_lost(
    history, server, tmp_path
):
    queue, service, repo, p, _, counts, _ = case(history, server, tmp_path, submit=False)
    binding = p.binding.model_copy(
        update={"server": p.binding.server.model_copy(update={"max_response_bytes": 2048})}
    )
    limits = BudgetLimits.model_validate(
        {**p.limits.model_dump(), "max_response_bytes": 2048, "max_artifact_bytes": 2048}
    )
    p = p.model_copy(update={"binding": binding, "limits": limits})
    submitted = service.submit(
        repo,
        p,
        actor="graph-operator",
        key="low-native-budget",
        branch_name="main",
        expected_head=None,
        expected_version=0,
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "failed" and result["error_code"] == "output_limit", result
    current, snapshot, _ = state(history, repo, UUID(submitted.run_id))
    assert current.error_code == "output_limit" and counts()["calls"] == 0
    assert (
        sum(len(history.objects.get(a.object_ref, ObjectType.BLOB)) for a in snapshot.artifacts)
        <= 2048
    )


@pytest.mark.parametrize(
    "point",
    [
        "langgraph.after_intent",
        "langgraph.after_result",
        "langgraph.after_fetch_writes",
        "langgraph.after_fetch_checkpoint",
    ],
)
def test_actual_graph_worker_death_consumes_durable_result_or_preserves_unknown(
    history, server, tmp_path, point
):
    queue, _, repo, _, submitted, counts, _ = case(history, server, tmp_path)
    run = UUID(submitted.run_id)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "graph-boundary.json"
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
            assert monotonic() < deadline, "实际图 Worker 未抵达指定边界"
            Event().wait(0.02)
        boundary = json.loads(marker.read_text("utf-8"))
        assert boundary["point"] == point and boundary["schema"] == schema
        assert boundary["pid"] == process.pid or boundary["parent_pid"] == process.pid
        assert (
            boundary["source_revision"] == source_revision()
            and boundary["lock_digest"] == dependency_lock_digest()
        )
        assert process.poll() is None
        if os.name == "nt":
            kill_interpreter(boundary["pid"])
        else:
            process.terminate()
        process.wait(timeout=10)
        if os.name == "nt":
            assert process.returncode == 73
        before = counts()
        with history.engine.begin() as connection:
            old_fence = connection.execute(
                sa.select(TABLES["jobs"].c.fencing_token).where(
                    TABLES["jobs"].c.id == UUID(submitted.job_id)
                )
            ).scalar_one()
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        recovered = Worker(queue, mcp_client_factory=forbidden).run_once()
        if point == "langgraph.after_intent":
            assert recovered["status"] == "outcome_unknown", recovered
            current, _, _ = state(history, repo, run)
            assert current.pending and current.actual_attempts is None
            assert counts()["calls"] == 0
            control(queue, repo, run, "cancel")
            assert Worker(queue).run_once() is None
        else:
            assert recovered["status"] == "paused", recovered
            control(queue, repo, run, "resume")
            completed = Worker(queue, mcp_client_factory=forbidden).run_once()
            assert completed["status"] == "completed", completed
            current, _, _ = state(history, repo, run)
            assert (
                current.usage.steps == 3 and current.actual_attempts == 1 and counts()["calls"] == 1
            )
        assert counts() == before
        with history.engine.connect() as connection:
            new_fence = connection.execute(
                sa.select(TABLES["jobs"].c.fencing_token).where(
                    TABLES["jobs"].c.id == UUID(submitted.job_id)
                )
            ).scalar_one()
        assert new_fence > old_fence
        (tmp_path / "process-evidence.json").write_text(
            json.dumps(
                {
                    "boundary": boundary,
                    "launcher_pid": process.pid,
                    "exit_code": process.returncode,
                    "before_counts": before,
                    "after_counts": counts(),
                    "before_fence": old_fence,
                    "after_fence": new_fence,
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
