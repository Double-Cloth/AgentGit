"""CLI 干预文件在 JSON 解析及管理写入前有界读取。"""

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agentgit.cli.main import app
from agentgit.services.management import ManagementService


@pytest.mark.parametrize("option", ["--overrides-file", "--budget-file"])
def test_live_cli_rejects_large_input_before_parsing_or_submission(tmp_path, monkeypatch, option):
    path = tmp_path / "large.json"
    path.write_bytes(b"x" * 1_048_577)
    manager = SimpleNamespace(history=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None)))
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    result = CliRunner().invoke(
        app,
        [
            "replay",
            "00000000-0000-0000-0000-000000000001",
            "--mode",
            "live",
            "--source-run",
            "00000000-0000-0000-0000-000000000002",
            "--from-commit",
            "a" * 64,
            "--branch-name",
            "large",
            "--key",
            "large",
            option,
            str(path),
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]["code"] == "output_limit"
