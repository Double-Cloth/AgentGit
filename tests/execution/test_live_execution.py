"""S5-T09：同一执行前检查点的 A/B 输入、容器和预算互相隔离。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_agent_flow import submit

from agentgit.adapters import mock
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, BudgetLimits
from agentgit.core.versions import ArtifactPatch, ExecutionOverride, LiveOverrides, LiveReplay
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("tool", ["code_exec", "terminal_exec"])
def test_live_ab_uses_fresh_containers_versioned_inputs_and_new_budgets(
    history, tmp_path, image_id, monkeypatch, tool
):
    monkeypatch.setattr(
        mock,
        "_STATISTICS_CODE",
        mock._STATISTICS_CODE + "\nPath('/tmp/source-only').write_text('private')\n",
    )
    queue, repository, source = submit(history, tmp_path)
    assert Worker(queue).run_once()["status"] == "completed"
    original = history.branch(repository)
    versions = VersionService(history)
    snapshots = [
        (item["commit_id"], history.restore(repository, item["commit_id"]))
        for item in reversed(versions.log(repository)["items"])
    ]
    checkpoint, snapshot = next(
        (commit, snapshot)
        for commit, snapshot in snapshots
        if snapshot.data.get("pending_tool", {}) is not None
        and snapshot.data.get("pending_tool", {}).get("tool") == tool
    )
    baseline = parse_contract(encode(snapshot.data), AgentState).usage
    assert baseline.tool_calls > 2
    source_files = {
        item.path: history.objects.get(item.object_ref, ObjectType.BLOB)
        for item in snapshot.artifacts
    }
    manager = ManagementService(queue.research)
    runs = []
    path = "execution/statistics.py" if tool == "code_exec" else "execution/organize.sh"
    for label in ("A", "B"):
        text = (
            "from pathlib import Path\n"
            "assert not Path('/tmp/source-only').exists()\n"
            "assert not Path('/tmp/live-only').exists()\n"
            "Path('/tmp/live-only').write_text('private')\n"
            "Path('results').mkdir(exist_ok=True)\n"
            f"Path('results/same.txt').write_text('{label}')\n"
            if tool == "code_exec"
            else "set -eu\n"
            "test ! -e /tmp/source-only\n"
            "test ! -e /tmp/live-only\n"
            "printf private > /tmp/live-only\n"
            "mkdir -p results\n"
            f"printf {label} > results/same.txt\n"
        )
        response = manager.replay(
            repository,
            LiveReplay(
                source_run_id=source.run_id,
                from_commit=checkpoint,
                branch_name=f"live-{label}",
                budget=BudgetLimits(max_tool_calls=3),
                overrides=LiveOverrides(
                    artifacts=(ArtifactPatch(path=path, text=text),),
                    execution=ExecutionOverride(tool=tool, path=path),
                ),
            ),
            f"live-{label}",
        )
        job_id, run_id = UUID(response["job_id"]), UUID(response["run_id"])
        assert not (queue.research.data_root / "workspaces" / str(run_id)).exists()
        result = Worker(manager.jobs).run_once()
        assert result["status"] == "completed", result
        current = manager.jobs.get(repository, job_id)
        output = history.restore(repository, current["checkpoint_commit_id"])
        files = {
            item.path: history.objects.get(item.object_ref, ObjectType.BLOB)
            for item in output.artifacts
        }
        assert files["results/same.txt"] == label.encode()
        assert files[path] == text.encode()
        report = manager.replay_status(repository, job_id)["replay"]
        expected = 3 if tool == "code_exec" else 1
        assert report["new_usage"]["tool_calls"] == expected
        assert report["actual"]["tool_calls"] == expected
        runs.append(run_id)
    with history.engine.connect() as connection:
        rows = list(connection.execute(sa.select(TABLES["sandbox_executions"])).mappings())
    assert len({row["container_id"] for row in rows}) == len(rows)
    assert all(row["image_id"] == image_id and row["cleanup_confirmed"] for row in rows)
    inputs = [{row["input_manifest_ref"] for row in rows if row["run_id"] == run} for run in runs]
    assert all(inputs) and inputs[0].isdisjoint(inputs[1])
    assert history.branch(repository) == original
    assert history.restore(repository, checkpoint) == snapshot
    assert {
        item.path: history.objects.get(item.object_ref, ObjectType.BLOB)
        for item in history.restore(repository, checkpoint).artifacts
    } == source_files
