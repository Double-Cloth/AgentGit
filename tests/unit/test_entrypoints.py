"""S1-T01/T06：入口、故障映射与执行准备边界。"""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from agentgit.adapters.execution.engine import EngineError
from agentgit.adapters.execution.policy import bundled_policy
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


def test_redirected_json_works_with_windows_legacy_encoding():
    env = {
        **os.environ,
        "PYTHONIOENCODING": "gbk",
        "AGENTGIT_DATABASE_URL": "postgresql+psycopg://u:secret@127.0.0.1:1/missing",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from agentgit.cli.main import app; app()", "doctor", "--database"],
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout.decode("utf-8"))["error"]["code"] == "database_unavailable"
    assert b"secret" not in result.stdout


def probe_engine(*, failure=None):
    class Probe:
        def __init__(self):
            self.requests = []

        def request(self, method, path):
            self.requests.append((method, path))
            assert method == "GET", "doctor 不能创建容器或执行命令"
            if failure == "engine":
                raise EngineError()
            if path == "/info":
                info = {
                    "OSType": "linux",
                    "CgroupVersion": "2",
                    "ServerVersion": "fixture",
                    **dict.fromkeys(
                        ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota", "CpuCfsPeriod"),
                        True,
                    ),
                    "SecurityOptions": ["name=seccomp,profile=builtin"],
                }
                if failure in ("windows", "cgroup", "memory", "seccomp"):
                    field, value = {
                        "windows": ("OSType", "windows"),
                        "cgroup": ("CgroupVersion", "1"),
                        "memory": ("MemoryLimit", False),
                        "seccomp": ("SecurityOptions", []),
                    }[failure]
                    info[field] = value
                return info
            assert path.startswith("/images/sha256:") and path.endswith("/json")
            if failure == "image":
                raise EngineError(status=404)
            return {"Id": bundled_policy().image_id, "Os": "linux"}

    return Probe()


@pytest.mark.parametrize(
    "failure", ["missing", "timeout", "engine", "windows", "cgroup", "memory", "seccomp", "image"]
)
def test_sandbox_probe_rejects_unavailable_prerequisites_without_side_effects(failure):
    engine = probe_engine(failure=failure)
    effect = {
        "missing": FileNotFoundError("secret-engine-details"),
        "timeout": subprocess.TimeoutExpired("docker", 10),
    }.get(failure)
    with patch("agentgit.diagnostics.DockerEngine.local", side_effect=effect, return_value=engine):
        report = sandbox_prerequisites()
    assert report["execution_ready"] is False
    assert report["code"] == "sandbox_unavailable"
    assert "secret-engine-details" not in json.dumps(report)


def test_sandbox_probe_accepts_only_pinned_image_and_complete_isolation_prerequisites():
    engine = probe_engine()
    with patch("agentgit.diagnostics.DockerEngine.local", return_value=engine):
        report = sandbox_prerequisites()
    assert report["execution_ready"] is True and report["code"] is None
    assert report["image_id"] == bundled_policy().image_id
    assert engine.requests == [
        ("GET", "/info"),
        ("GET", f"/images/{bundled_policy().image_id}/json"),
        ("GET", "/info"),
    ]


def test_doctor_sandbox_returns_nonzero_when_prerequisites_fail():
    with patch("agentgit.cli.main.sandbox_prerequisites", return_value={"execution_ready": False}):
        result = CliRunner().invoke(app, ["doctor", "--sandbox"])
    assert result.exit_code == 1
