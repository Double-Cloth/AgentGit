"""验证控制器的公共查询、真实进程退出与完整原字节审计。"""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from test_management_api import TOKEN
from test_repair_confirmation import actual_worker, drive_confirmation, setup_confirmation
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.services.management import ManagementService
from agentgit.services.repair_confirmation import TABLES, RepairConfirmationService

pytestmark = pytest.mark.integration


def test_full_cas_audit_recounts_new_model_bytes_and_preserves_equal_outputs(history, tmp_path):
    repo, _, research, source, reduced = setup_confirmation(history, tmp_path, mode="joint")
    result = drive_confirmation(history, research, repo)
    assert result["status"] == "verified_under_fixture", result
    assert len(reduced["final_lock"]["lock"]["candidate"]["atoms"]) == 2
    bundle = capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-two-atom-six-plus-four/v1",
        environment="AGENTGIT_S10_CONFIRMATION_FULL_EVIDENCE",
    )
    assert bundle["raw_audit"]["model_calls"] > 10


def test_process_exit_after_dispatch_reuses_job_before_cancellation(history, tmp_path):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("repair_confirmation_process.py")),
            schema,
            str(history.objects.root),
            str(research.data_root),
            str(repo),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stdout + child.stderr
    control = RepairConfirmationService(research)
    before = control.view(repo, study)
    assert before["usage"]["active"] == 1 and len(before["trials"]) == 1
    runner = RepairConfirmationWorker(research)
    resumed = runner.run_once(repository=repo)
    assert resumed["trials"] == before["trials"]
    worker = actual_worker(history, research, repo, "pass")
    assert worker.run_once()["status"] == "completed"
    assert len(runner.run_once(repository=repo)["trials"]) == 2
    control.ledger.cancel(repo, study, actor="operator")
    assert RepairStudyWorker(research).run_once(repository=repo) is None
    runner.run_once(repository=repo)
    assert worker.run_once()["status"] == "cancelled"
    assert runner.run_once(repository=repo)["status"] == "cancelled"


def test_http_cli_read_settle_and_cancel_do_not_invoke_adapter_or_accept_verdict(
    history, tmp_path, monkeypatch
):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    manager = ManagementService(research)

    @contextmanager
    def current(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", current)

    def forbidden(*args, **kwargs):
        pytest.fail("控制、结算及查询不得调用模型/工具")

    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        with monkeypatch.context() as no_execution:
            no_execution.setattr(MockResearchModel, "invoke", forbidden)
            no_execution.setattr(ToolRegistry, "invoke", forbidden)
            dispatched = CliRunner().invoke(
                app, ["repair-confirm-worker", "--repository-id", str(repo), "--once"]
            )
            assert dispatched.exit_code == 0, dispatched.output
            assert json.loads(dispatched.output)["status"] == "confirming"
            before = client.get(f"/repair-studies/{study}/validation").json()["execution"]
            assert before["verification"] is None
            assert (
                client.get(
                    f"/repair-studies/{study}/validation", headers={"X-Repository-ID": str(uuid4())}
                ).status_code
                == 404
            )
            assert (
                client.post(
                    f"/repair-studies/{study}/open-validation",
                    json={"schema_version": 1, "suite_id": str(uuid4()), "passed": True},
                    headers={"Idempotency-Key": "fake"},
                ).status_code
                == 422
            )
        assert actual_worker(history, research, repo, "pass").run_once()["status"] == "completed"
        with monkeypatch.context() as no_execution:
            no_execution.setattr(MockResearchModel, "invoke", forbidden)
            no_execution.setattr(ToolRegistry, "invoke", forbidden)
            settled = CliRunner().invoke(
                app, ["repair-confirm-worker", "--repository-id", str(repo), "--once"]
            )
            assert settled.exit_code == 0, settled.output
            current_view = client.get(f"/repair-studies/{study}/validation").json()["execution"]
            assert current_view["provisional_verification"]["confirm_attempted"] == 1
            assert len(current_view["trials"]) == 2
            with monkeypatch.context() as readonly:
                readonly.setattr(history.objects, "put", forbidden)
                assert client.get(f"/repair-studies/{study}/validation").status_code == 200
            assert (
                client.post(
                    f"/repair-studies/{study}/cancel",
                    json={},
                    headers={"Idempotency-Key": "cancel"},
                ).status_code
                == 200
            )
            assert RepairStudyWorker(research).run_once(repository=repo) is None
        runner = RepairConfirmationWorker(research)
        runner.run_once(repository=repo)
        assert actual_worker(history, research, repo, "pass").run_once()["status"] == "cancelled"
        final = runner.run_once(repository=repo)
        assert final["status"] == "cancelled" and final["verification"]["status"] == "needs_review"
    for name in (
        "repair_validation_trials",
        "repair_validation_jobs",
        "repair_validation_results",
        "repair_validation_summaries",
    ):
        with history.engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(TABLES[name].delete())
