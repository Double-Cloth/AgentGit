"""CLI 与 HTTP 使用同一管理事务与幂等记录。"""

import json
from uuid import UUID

import pytest
from test_management_api import api as api
from typer.testing import CliRunner

from agentgit.cli.main import app
from agentgit.services.management import ManagementService

pytestmark = pytest.mark.integration


def invoke(arguments):
    response = CliRunner().invoke(app, arguments)
    assert response.exit_code == 0, (response.stdout, response.exception)
    return json.loads(response.stdout)


def test_cli_lifecycle_idempotency_and_worker(api, monkeypatch):
    _, service, _ = api
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    created = invoke(["init", "命令行研究", "--key", "cli-root"])
    assert invoke(["init", "命令行研究", "--key", "cli-root"]) == created
    repo = created["repository_id"]
    config = invoke(["config", repo, "--expected-version", "0", "--key", "cli-config"])
    task = invoke(
        [
            "task",
            repo,
            config["config_id"],
            "比较三个 fixture 框架",
            "--config-version",
            "1",
            "--key",
            "cli-task",
        ]
    )
    run = invoke(
        [
            "run",
            repo,
            task["task_id"],
            "--config-version",
            "1",
            "--expected-branch-version",
            "0",
            "--key",
            "cli-run",
        ]
    )
    paused = invoke(["pause", repo, run["run_id"], "--expected-version", "0", "--key", "cli-pause"])
    assert paused["control_status"] == "requested"
    assert invoke(["worker", "--once"])["status"] == "paused"
    current = invoke(["status", repo, run["run_id"]])
    resumed = invoke(
        [
            "resume",
            repo,
            run["run_id"],
            "--expected-version",
            str(current["state_version"]),
            "--key",
            "cli-resume",
        ]
    )
    assert resumed["creates_new_run"] is False
    assert invoke(["worker", "--once"])["status"] == "completed"
    assert invoke(["status", repo, run["job_id"], "--job"])["status"] == "completed"
    assert len(invoke(["artifacts", repo, run["run_id"]])["items"]) == 2
    assert invoke(["approvals", repo, run["run_id"]])["items"] == []
    assert invoke(["worker", "--once"])["status"] == "idle"
    assert service.jobs.run(UUID(repo), UUID(run["run_id"]))["status"] == "completed"


def test_cli_budget_failure_is_one_json_and_nonzero(api, monkeypatch, tmp_path):
    _, service, _ = api
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: service)
    repo = invoke(["init", "受限任务", "--key", "limited-root"])["repository_id"]
    config_file = tmp_path / "limited.json"
    config_file.write_text(
        json.dumps({"schema_version": 1, "limits": {"schema_version": 1, "max_steps": 1}}),
        encoding="utf-8",
    )
    config = invoke(
        [
            "config",
            repo,
            "--expected-version",
            "0",
            "--file",
            str(config_file),
            "--key",
            "limited-config",
        ]
    )
    task = invoke(
        [
            "task",
            repo,
            config["config_id"],
            "比较三个 fixture 框架",
            "--config-version",
            "1",
            "--key",
            "limited-task",
        ]
    )
    invoke(
        [
            "run",
            repo,
            task["task_id"],
            "--config-version",
            "1",
            "--expected-branch-version",
            "0",
            "--key",
            "limited-run",
        ]
    )
    result = CliRunner().invoke(app, ["worker", "--once"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "budget_exhausted"


def test_cli_parameter_errors_are_json():
    result = CliRunner().invoke(app, ["status", "not-an-id", "private-input"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]["code"] == "validation"
    assert "private-input" not in result.stdout
