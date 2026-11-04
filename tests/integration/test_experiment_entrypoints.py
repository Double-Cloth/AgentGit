"""S7：API/CLI 复用冻结输入、幂等排队与真实 Worker 状态。"""

import json

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_recorded_replayer import replay_source as replay_source
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.research import BudgetLimits
from agentgit.services.management import ManagementService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def client_for(manager, repository_id):
    token = "fixture-experiment-token-0000000000000000000000000"
    return TestClient(
        create_app(Settings(api_token=SecretStr(token)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(repository_id)},
    )


def test_api_cli_share_freeze_and_experiment_idempotency_and_real_worker(
    history,
    replay_source,
    tmp_path,
    monkeypatch,
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    with client_for(manager, session.repository_id) as client:
        body = {
            "schema_version": 1,
            "source_run_id": str(session.run_id),
            "from_commit": commits[15],
        }
        frozen = client.post("/validators", json=body, headers={"Idempotency-Key": "freeze-shared"})
        assert frozen.status_code == 201, frozen.text
        cli = CliRunner().invoke(
            app,
            [
                "freeze-validator",
                str(session.repository_id),
                "--source-run",
                str(session.run_id),
                "--from-commit",
                commits[15],
                "--key",
                "freeze-shared",
            ],
        )
        assert cli.exit_code == 0, (cli.stdout, cli.exception)
        assert json.loads(cli.stdout) == frozen.json()
        ref = frozen.json()["validator_ref"]
        assert client.get(f"/validators/{ref}").json()["manifest"]["rules"]
        request = NewExperiment(
            source_run_id=str(session.run_id),
            from_commit=commits[15],
            validator_ref=ref,
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="原配置"),
                ExperimentGroup(
                    key="B", label="核验引用", prompt=session.config.prompt + "核验引用。"
                ),
            ),
        )
        created = client.post(
            "/experiments",
            json=request.model_dump(mode="json"),
            headers={
                "Idempotency-Key": "experiment-shared",
            },
        )
        assert created.status_code == 202, created.text
        path = tmp_path / "experiment.json"
        path.write_bytes(encode(request.model_dump(mode="json")))
        cli = CliRunner().invoke(
            app,
            [
                "experiment",
                str(session.repository_id),
                "--file",
                str(path),
                "--key",
                "experiment-shared",
            ],
        )
        assert cli.exit_code == 0, (cli.stdout, cli.exception)
        assert json.loads(cli.stdout) == created.json()
        experiment_id = created.json()["experiment_id"]
        waiting = client.get(f"/experiments/{experiment_id}").json()
        assert all(trial["status"] == "queued" for trial in waiting["trials"])
        assert all(trial["verdict"] == "incomplete" for trial in waiting["trials"])
        worker = Worker(manager.jobs)
        assert worker.run_once()["status"] == "completed"
        assert worker.run_once()["status"] == "completed"
        complete = client.get(f"/experiments/{experiment_id}")
        assert complete.status_code == 200, complete.text
        assert all(trial["verdict"] == "passed" for trial in complete.json()["trials"])
        cli = CliRunner().invoke(
            app, ["experiment-status", str(session.repository_id), experiment_id]
        )
        assert cli.exit_code == 0, (cli.stdout, cli.exception)
        assert json.loads(cli.stdout) == complete.json()
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["jobs"])
                ).scalar_one()
                == 2
            )


def test_new_routes_reject_cross_project_unauthorized_unknown_fields_and_large_input(
    history,
    replay_source,
    tmp_path,
    monkeypatch,
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    with client_for(manager, session.repository_id) as client:
        body = {
            "schema_version": 1,
            "source_run_id": str(session.run_id),
            "from_commit": commits[15],
        }
        assert (
            client.post(
                "/validators",
                json={**body, "rules": ["always_pass"]},
                headers={
                    "Idempotency-Key": "bad-rules",
                },
            ).status_code
            == 422
        )
        ref = client.post(
            "/validators", json=body, headers={"Idempotency-Key": "owned-validator"}
        ).json()["validator_ref"]
        request = NewExperiment(
            source_run_id=str(session.run_id),
            from_commit=commits[15],
            validator_ref=ref,
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="A"),
                ExperimentGroup(key="B", label="B"),
            ),
        ).model_dump(mode="json")
        bad = {
            **request,
            "groups": [request["groups"][0], {**request["groups"][1], "tools": ["host_shell"]}],
        }
        assert (
            client.post(
                "/experiments", json=bad, headers={"Idempotency-Key": "bad-group"}
            ).status_code
            == 422
        )
        created = client.post(
            "/experiments", json=request, headers={"Idempotency-Key": "owned-experiment"}
        ).json()
        other = history.create_repository("其他项目")
        for endpoint in (f"/validators/{ref}", f"/experiments/{created['experiment_id']}"):
            assert client.get(endpoint, headers={"X-Repository-ID": str(other)}).status_code == 404
            assert (
                client.get(endpoint, headers={"Authorization": "Bearer wrong"}).status_code == 401
            )
        huge = tmp_path / "huge.json"
        huge.write_bytes(b"x" * 1_048_577)
        cli = CliRunner().invoke(
            app,
            [
                "experiment",
                str(session.repository_id),
                "--file",
                str(huge),
                "--key",
                "huge",
            ],
        )
        assert cli.exit_code != 0
        assert json.loads(cli.stdout)["error"]["code"] == "output_limit"
        assert (
            client.post(
                "/experiments",
                content=huge.read_bytes(),
                headers={
                    "Idempotency-Key": "huge",
                    "Content-Type": "application/json",
                },
            ).status_code
            == 413
        )
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["jobs"])
                ).scalar_one()
                == 2
            )
