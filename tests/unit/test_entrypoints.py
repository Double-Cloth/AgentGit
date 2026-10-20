"""S1-T01/T06：入口、故障映射与执行准备边界。"""

import json
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.diagnostics import sandbox_prerequisites


def test_version_and_health_without_database():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["agentgit"] == "0.1.0"
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unavailable_database_does_not_leak_credentials(monkeypatch):
    url = "postgresql+psycopg://user:private-password@127.0.0.1:1/unavailable"
    config = Settings(database_url=SecretStr(url), connect_timeout_seconds=1)
    response = TestClient(create_app(config)).get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "private-password" not in response.text
    monkeypatch.setenv("AGENTGIT_DATABASE_URL", url)
    result = CliRunner().invoke(app, ["doctor", "--database"])
    assert result.exit_code == 1
    assert "private-password" not in result.stdout
    assert json.loads(result.stdout)["error"]["code"] == "database_unavailable"


@pytest.mark.parametrize(
    "effect,result",
    [
        (FileNotFoundError(), None),
        (subprocess.TimeoutExpired("docker", 10), None),
        (None, subprocess.CompletedProcess([], 1, "", "secret-engine-details")),
        (None, subprocess.CompletedProcess([], 0, '{"OSType":"windows"}', "")),
        (None, subprocess.CompletedProcess([], 0, "invalid-json", "")),
        (None, subprocess.CompletedProcess([], 0, '{"OSType":"linux"}', "")),
    ],
)
def test_sandbox_is_only_prerequisite_probe(effect, result):
    with patch(
        "agentgit.diagnostics.subprocess.run", side_effect=effect, return_value=result
    ) as run:
        report = sandbox_prerequisites()
    assert report["execution_ready"] is False
    assert report["code"] == "sandbox_unavailable"
    assert "secret-engine-details" not in json.dumps(report)
    assert run.call_args.args[0] == ["docker", "info", "--format", "{{json .}}"]


def test_doctor_sandbox_returns_nonzero_until_execution_verified():
    with patch("agentgit.cli.main.sandbox_prerequisites", return_value={"execution_ready": False}):
        result = CliRunner().invoke(app, ["doctor", "--sandbox"])
    assert result.exit_code == 1
