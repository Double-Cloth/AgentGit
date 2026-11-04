"""独立图 Fork 的真实来源/分支/Job、原生备注、零取证和原子拒绝边界。"""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_langgraph_runs import archive as archive
from test_langgraph_runs import case, control, forbidden, state
from test_mcp_adapter import server as server
from test_merge_process_crash import kill_interpreter
from typer.testing import CliRunner

from agentgit.cli.main import app
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph_runs import LangGraphRunState, NewLangGraphFork
from agentgit.core.models import parse_contract
from agentgit.core.research import BudgetLimits
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def ready(history, server, tmp_path):
    queue, service, repo, p, receipt, counts, mode = case(history, server, tmp_path)
    assert Worker(queue).run_once()["status"] == "paused"
    run = UUID(receipt.run_id)
    current, _, row = state(history, repo, run)
    return queue, service, repo, p, run, row["checkpoint_commit_id"], current, counts


def fork_request(run, checkpoint, name="review-fork", note="独立分支补充说明"):
    return NewLangGraphFork(
        source_run_id=str(run),
        from_commit=checkpoint,
        name=name,
        expected_version=0,
        review_note=note,
    )


def fingerprint(history, queue, repo, run):
    row = queue.run(repo, run)
    return encode(
        {
            "run": {str(key): str(value) for key, value in row.items()},
            "branch": {
                str(key): str(value)
                for key, value in history.branch(repo, row["branch_name"]).items()
            },
            "snapshot": history.restore(repo, row["checkpoint_commit_id"]).model_dump(mode="json"),
            "events": [
                {str(key): str(value) for key, value in event.items()}
                for event in history.events(repo, run)
            ],
        }
    )


def test_fork_edits_note_in_independent_run_and_preserves_entire_source(history, server, tmp_path):
    queue, service, repo, _, source, checkpoint, original, counts = ready(history, server, tmp_path)
    before = fingerprint(history, queue, repo, source)
    before_counts = counts()
    request = fork_request(source, checkpoint)
    receipt = service.fork(repo, request, actor="graph-operator", key="fork-once")
    assert receipt.run_id != str(source)
    result = Worker(queue, mcp_client_factory=forbidden, model_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    current, snapshot, row = state(history, repo, UUID(receipt.run_id))
    assert current.fork_initialized and current.actual_attempts == 0
    assert current.usage.steps == 3 and current.usage.tool_calls == 1
    assert current.trace == original.trace
    assert row["origin_run_id"] == source and row["base_commit_id"] == checkpoint
    assert (
        current.values["report"]
        == "# 取证报告\n\ngraph-fixture-value\n\n审阅备注：独立分支补充说明\n"
    )
    assert {a.path for a in snapshot.artifacts} == {"langgraph-native.json", "report.md"}
    assert fingerprint(history, queue, repo, source) == before and counts() == before_counts
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["external_calls"])
                .where(TABLES["external_calls"].c.run_id == UUID(receipt.run_id))
            ).scalar_one()
            == 0
        )
    assert service.fork(repo, request, actor="graph-operator", key="fork-once") == receipt


def test_historical_review_checkpoint_remains_usable_after_source_completes(
    history, server, tmp_path
):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    control(queue, repo, source, "resume")
    assert Worker(queue).run_once()["status"] == "completed"
    before = fingerprint(history, queue, repo, source)
    receipt = service.fork(
        repo, fork_request(source, checkpoint), actor="graph-operator", key="historical"
    )
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    current, _, row = state(history, repo, UUID(receipt.run_id))
    assert row["base_commit_id"] == checkpoint and current.actual_attempts == 0
    assert fingerprint(history, queue, repo, source) == before and counts()["calls"] == 1


def test_fork_of_fork_uses_original_proven_response_without_refetch(history, server, tmp_path):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    first = service.fork(
        repo,
        fork_request(source, checkpoint, name="first", note="第一备注"),
        actor="graph-operator",
        key="first",
    )
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    first_run = UUID(first.run_id)
    with history.engine.connect() as connection:
        commits = list(
            connection.execute(
                sa.select(TABLES["snapshots"])
                .where(TABLES["snapshots"].c.run_id == first_run)
                .order_by(TABLES["snapshots"].c.event_seq)
            ).mappings()
        )
    safe = next(
        row["commit_id"]
        for row in commits
        if (
            candidate := parse_contract(
                encode(history.restore(repo, row["commit_id"]).data), LangGraphRunState
            )
        ).fork_initialized
        and candidate.native_operation == "checkpoint"
        and candidate.native_next == ("review",)
    )
    before = fingerprint(history, queue, repo, first_run)
    second = service.fork(
        repo,
        fork_request(first_run, safe, name="second", note="第二备注"),
        actor="graph-operator",
        key="second",
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    current, _, _ = state(history, repo, UUID(second.run_id))
    assert "第二备注" in current.values["report"] and "第一备注" not in current.values["report"]
    assert current.actual_attempts == 0 and counts()["calls"] == 1
    assert fingerprint(history, queue, repo, first_run) == before


def test_fork_key_and_existing_branch_conflicts_are_atomic(history, server, tmp_path):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    request = fork_request(source, checkpoint)
    receipt = service.fork(repo, request, actor="graph-operator", key="exact")
    assert service.fork(repo, request, actor="graph-operator", key="exact") == receipt
    for key, changed in [
        ("exact", request.model_copy(update={"review_note": "换输入"})),
        ("different", request),
    ]:
        with pytest.raises(AgentGitError) as caught:
            service.fork(repo, changed, actor="graph-operator", key=key)
        assert caught.value.code == ErrorCode.VERSION_CONFLICT
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branches"])
            ).scalar_one()
            == 2
        )
    assert counts()["calls"] == 1


def test_incomplete_and_completed_graph_points_cannot_create_fork(history, server, tmp_path):
    queue, service, repo, _, source, _, _, counts = ready(history, server, tmp_path)
    control(queue, repo, source, "resume")
    assert Worker(queue).run_once()["status"] == "completed"
    with history.engine.connect() as connection:
        rows = list(
            connection.execute(
                sa.select(TABLES["snapshots"]).where(TABLES["snapshots"].c.run_id == source)
            ).mappings()
        )
    candidates = {}
    for row in rows:
        current = parse_contract(
            encode(history.restore(repo, row["commit_id"]).data), LangGraphRunState
        )
        if current.native_ref is None:
            candidates["empty"] = row["commit_id"]
        if current.pending:
            candidates["pending"] = row["commit_id"]
        if current.native_operation == "writes" and current.values.get("node_order") == ["fetch"]:
            candidates["writes"] = row["commit_id"]
        if current.status.value == "completed":
            candidates["completed"] = row["commit_id"]
    assert set(candidates) == {"empty", "pending", "writes", "completed"}
    for name, point in candidates.items():
        with pytest.raises(AgentGitError):
            service.fork(
                repo, fork_request(source, point, name=name), actor="graph-operator", key=name
            )
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branches"])
            ).scalar_one()
            == 1
        )
    assert counts()["calls"] == 1


def test_cross_project_and_mid_transaction_failure_leave_no_fork_rows(
    history, server, tmp_path, monkeypatch
):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError):
        service.fork(other, fork_request(source, checkpoint), actor="graph-operator", key="foreign")
    original = history._ref

    def fail(connection, repository_id, item, owner_type, owner_id, role):
        if owner_type == "job":
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "测试事务中断")
        return original(connection, repository_id, item, owner_type, owner_id, role)

    monkeypatch.setattr(history, "_ref", fail)
    with pytest.raises(AgentGitError):
        service.fork(repo, fork_request(source, checkpoint), actor="graph-operator", key="rollback")
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branch_forks"])
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["runs"])).scalar_one()
            == 1
        )
    assert counts()["calls"] == 1


def test_parallel_fork_same_key_returns_single_atomic_job(history, server, tmp_path):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    request = fork_request(source, checkpoint)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: service.fork(repo, request, actor="graph-operator", key="parallel"),
                range(2),
            )
        )
    assert results[0] == results[1]
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    assert counts()["calls"] == 1
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branch_forks"])
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 2
        )


@pytest.mark.parametrize("field", ["program", "native_ref", "lineage"])
def test_forged_fork_job_cannot_change_budget_native_source_or_lineage(
    history, server, tmp_path, field
):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    before = fingerprint(history, queue, repo, source)
    receipt = service.fork(
        repo, fork_request(source, checkpoint), actor="graph-operator", key="forged"
    )
    job = queue.get(repo, UUID(receipt.job_id))
    payload = history.objects.get_json(job["payload_ref"], ObjectType.BLOB)
    if field == "program":
        program = history.objects.get_json(payload["program_ref"], ObjectType.BLOB)
        program["limits"]["max_steps"] = 4
        payload["program_ref"] = history.register_input(repo, encode(program))
    elif field == "native_ref":
        payload["fork"]["native_ref"] = "0" * 64
    else:
        payload["fork"]["source_run_id"] = receipt.run_id
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
    assert fingerprint(history, queue, repo, source) == before and counts()["calls"] == 1
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["snapshots"])
                .where(TABLES["snapshots"].c.run_id == UUID(receipt.run_id))
            ).scalar_one()
            == 0
        )


def test_corrupted_native_cas_is_rejected_before_fork_rows(history, server, tmp_path):
    queue, service, repo, _, source, checkpoint, original, counts = ready(history, server, tmp_path)
    path = next(path for key, path in history.objects.inventory() if key == original.native_ref)
    raw = path.read_bytes()
    try:
        path.write_bytes(raw[:-1] + b"X")
        with pytest.raises(AgentGitError) as caught:
            service.fork(
                repo, fork_request(source, checkpoint), actor="graph-operator", key="corrupt"
            )
        assert caught.value.code == ErrorCode.OBJECT_CORRUPT
    finally:
        path.write_bytes(raw)
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 1
        )
    assert counts()["calls"] == 1


def test_fork_note_obeys_original_context_budget_without_new_fetch(history, server, tmp_path):
    queue, service, repo, p, _, counts, _ = case(history, server, tmp_path, submit=False)
    p = p.model_copy(
        update={
            "limits": BudgetLimits.model_validate(
                {**p.limits.model_dump(), "max_context_bytes": 128}
            )
        }
    )
    source = service.submit(
        repo,
        p,
        actor="graph-operator",
        key="limited-source",
        branch_name="main",
        expected_head=None,
        expected_version=0,
    )
    source_run = UUID(source.run_id)
    assert Worker(queue).run_once()["status"] == "paused"
    _, _, row = state(history, repo, source_run)
    receipt = service.fork(
        repo,
        fork_request(source_run, row["checkpoint_commit_id"], note="受限" * 60),
        actor="graph-operator",
        key="limited-fork",
    )
    result = Worker(queue, mcp_client_factory=forbidden).run_once()
    assert result["status"] == "failed" and result["error_code"] == "output_limit", result
    current, _, _ = state(history, repo, UUID(receipt.run_id))
    assert (
        current.fork_initialized and current.actual_attempts == 0 and current.values["report"] == ""
    )
    assert counts()["calls"] == 1


def test_fork_cli_and_status_expose_same_source_and_zero_actual_calls(
    history, server, tmp_path, monkeypatch
):
    queue, _, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    service = ManagementService(queue.research, actor="graph-operator")
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    path = tmp_path / "graph-fork.json"
    path.write_bytes(encode(fork_request(source, checkpoint).model_dump(mode="json")))
    runner = CliRunner()
    args = ["langgraph-fork", str(repo), str(path), "--key", "cli-fork", "--testing"]
    response = runner.invoke(app, args)
    assert response.exit_code == 0, response.output
    receipt = json.loads(response.output)
    assert json.loads(runner.invoke(app, args).output) == receipt
    assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    view = service.run(repo, UUID(receipt["run_id"]))
    assert view["adapter"]["fork"]["source_run_id"] == str(source)
    assert view["adapter"]["actual_attempts"] == 0 and view["adapter"]["fork_initialized"]
    assert counts()["calls"] == 1


@pytest.mark.parametrize(
    "point",
    ["langgraph.after_fork_seed", "langgraph.after_fork_writes", "langgraph.after_fork_checkpoint"],
)
def test_actual_fork_worker_death_preserves_source_and_applies_note_without_fetch(
    history, server, tmp_path, point
):
    queue, service, repo, _, source, checkpoint, _, counts = ready(history, server, tmp_path)
    before_source = fingerprint(history, queue, repo, source)
    receipt = service.fork(
        repo, fork_request(source, checkpoint), actor="graph-operator", key="process-fork"
    )
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "fork-boundary.json"
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
            assert monotonic() < deadline, "Fork Worker 未抵达确切边界"
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
        assert (
            current.fork_initialized
            and current.actual_attempts == 0
            and "独立分支补充说明" in current.values["report"]
        )
        assert counts() == before_counts and counts()["calls"] == 1
        assert fingerprint(history, queue, repo, source) == before_source
        with history.engine.connect() as connection:
            new = connection.execute(
                sa.select(TABLES["jobs"].c.fencing_token).where(
                    TABLES["jobs"].c.id == UUID(receipt.job_id)
                )
            ).scalar_one()
        assert new > old
        (tmp_path / "fork-process-evidence.json").write_text(
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
