"""缩减公共入口、独立进程中断和 Hybrid 自有输入的真实边界。"""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN
from test_repair_studies import begin, counts, drive_study, screening
from test_repair_worker import ConditionalMock, create_case, drive
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ModelRequest
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_studies import TABLES, RepairStudyService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_http_and_cli_reduce_reuse_strict_input_and_only_worker_executes(
    history, tmp_path, monkeypatch
):
    repo, search, research, source = screening(history, tmp_path, mode="prompt")
    manager = ManagementService(research)
    body = {"schema_version": 1, "search_id": str(search)}
    headers = {"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)}

    def forbidden(*args, **kwargs):
        pytest.fail("公共提交、读取和拒绝请求不能执行模型或工具")

    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers=headers,
    ) as client:
        before = counts(history)
        key = {"Idempotency-Key": "http-reduce"}
        with monkeypatch.context() as control:
            control.setattr(MockResearchModel, "invoke", forbidden)
            control.setattr(ToolRegistry, "invoke", forbidden)
            assert client.post("/repair-studies", json=body).status_code == 422
            assert (
                client.post(
                    "/repair-studies", json=body, headers={**key, "Authorization": "Bearer invalid"}
                ).status_code
                == 401
            )
            for field in ("passed", "result", "usage", "candidate", "validator_ref"):
                response = client.post("/repair-studies", json={**body, field: {}}, headers=key)
                assert response.status_code == 422 and response.headers["X-Correlation-ID"]
            response = client.post(
                "/repair-studies", json=body, headers={**key, "X-Repository-ID": str(uuid4())}
            )
            assert response.status_code == 404
            assert counts(history) == before
            response = client.post("/repair-studies", json=body, headers=key)
            assert response.status_code == 202, response.text
            created = response.json()
            assert client.post("/repair-studies", json=body, headers=key).json() == created
            assert (
                client.post(
                    "/repair-studies", json={**body, "max_reduce_trials": 0}, headers=key
                ).status_code
                == 409
            )
            assert counts(history)["jobs"] == before["jobs"]
        study = UUID(created["study_id"])
        result = drive_study(research, repo)
        assert result["status"] == "awaiting_samples"
        assert result["usage"]["started"]["reduce"] == 4
        assert result["field_reduction"]["summary"]["accepted_fields"] == 1
        assert result["field_reduction"]["summary"]["field_minimality"] == (
            "one_step_minimal_under_fixture"
        )
        assert result["usage"]["started"]["confirm"] == 0

        @contextmanager
        def current(testing):
            yield manager

        monkeypatch.setattr("agentgit.cli.main.management", current)
        with monkeypatch.context() as read:
            read.setattr(MockResearchModel, "invoke", forbidden)
            read.setattr(ToolRegistry, "invoke", forbidden)
            read.setattr(history.objects, "put", forbidden)
            fetched = client.get(f"/repair-studies/{study}")
            assert (
                fetched.status_code == 200 and fetched.json()["final_lock"] == result["final_lock"]
            )
            assert (
                client.get(
                    f"/repair-studies/{study}", headers={"X-Repository-ID": str(uuid4())}
                ).status_code
                == 404
            )
            cli = CliRunner().invoke(app, ["repair-study-status", str(repo), str(study)])
            assert cli.exit_code == 0, cli.output
            assert json.loads(cli.output)["final_lock"] == result["final_lock"]
        cli = CliRunner().invoke(
            app, ["repair-reduce", str(repo), str(search), "--key", "http-reduce"]
        )
        assert cli.exit_code == 0 and json.loads(cli.output) == created
        cancelled = client.post(
            f"/repair-studies/{study}/cancel", json={}, headers={"Idempotency-Key": "stop"}
        )
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert (
            client.post(
                f"/repair-studies/{study}/cancel", json={}, headers={"Idempotency-Key": "stop"}
            ).json()
            == cancelled.json()
        )
        assert RepairStudyService(history).view(repo, study)["final_lock"] == result["final_lock"]
        assert RepairJobService(history).view(repo, search)["decision"] == source["decision"]


def test_controller_process_exit_after_dispatch_reuses_job_and_physical_charge(history, tmp_path):
    repo, study, _, research, ledger, _ = begin(history, tmp_path, mode="prompt")
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("repair_study_process.py")),
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
    before = ledger.view(repo, study)
    assert before["usage"]["active"] == 1 and len(before["trials"]) == 1
    resumed = RepairStudyWorker(research).run_once(repository=repo)
    assert resumed["trials"] == before["trials"]
    assert (
        Worker(JobService(research), model_factory=ConditionalMock).run_once()["status"] == "failed"
    )
    result = drive_study(research, repo)
    assert result["status"] == "awaiting_samples" and result["usage"]["active"] == 0
    assert (
        len(result["trials"]) == 1
        and result["trials"][0]["dispatch"] == before["trials"][0]["dispatch"]
    )


def test_hybrid_reduction_freezes_own_policy_and_reports_logical_and_actual_separately(
    history, tmp_path
):
    repo, old_search, research, session = create_case(history, tmp_path, mode="prompt")
    ledger = RepairJobService(history)
    original = ledger.view(repo, old_search)["manifest"]
    ledger.cancel(repo, old_search, actor="fixture")
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == session.run_id,
                    TABLES["external_calls"].c.logical_step_id == "read-5",
                )
            )
            .mappings()
            .one()
        )
        for model_call in connection.execute(
            sa.select(TABLES["external_calls"]).where(
                TABLES["external_calls"].c.run_id == session.run_id
            )
        ).mappings():
            intent = parse_contract(
                history.objects.get(model_call["intent_ref"], ObjectType.CALL), CallIntent
            )
            if not intent.occurrence.provider.startswith("llm:"):
                continue
            request = parse_contract(
                history.objects.get(intent.request_ref, ObjectType.BLOB), ModelRequest
            )
            ready = request.state.plan.ready(request.state.completed) if request.state.plan else ()
            if request.phase == "act" and ready and ready[0].key == "read-5":
                event = connection.execute(
                    sa.select(TABLES["events"].c.id).where(
                        TABLES["events"].c.run_id == session.run_id,
                        TABLES["events"].c.logical_step_id == model_call["logical_step_id"],
                        TABLES["events"].c.type == "llm.requested",
                    )
                ).scalar_one()
                base = intent.checkpoint
                break
        else:
            pytest.fail("没有找到只读观测之前的模型决策")
    manager = ManagementService(research)
    created = manager.create_repair_search(
        repo,
        NewRepairSearch.model_validate_json(
            encode(
                {
                    "schema_version": 1,
                    "failed_run_id": str(session.run_id),
                    "base_commit": base,
                    "validator_ref": original["space"]["binding"]["validator_ref"],
                    "targets": [
                        {
                            "schema_version": 1,
                            "event_id": event,
                        }
                    ],
                    "hybrid": {
                        "schema_version": 1,
                        "until_commit": session.service.history.branch(repo)["head_commit_id"],
                        "calls": [str(call["id"])],
                    },
                }
            )
        ),
        "hybrid-screen",
    )
    search = UUID(created["search_id"])
    assert drive(research, repo, search)["status"] == "needs_confirmation"
    studies = RepairStudyService(history)
    made = studies.submit(
        repo,
        NewRepairStudy(search_id=str(search), field_policy=None),
        actor="fixture",
        key="hybrid-reduce",
    )
    result = drive_study(research, repo)
    assert result["study_id"] == made["study_id"] and result["status"] == "awaiting_samples"
    trial = result["trials"][0]
    assert trial["intent"]["hybrid_ref"] and trial["result"]["verdict"] == "failed"
    with history.engine.connect() as connection:
        refs = TABLES["object_refs"]
        assert (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repo,
                    refs.c.owner_type == "repair_reduction_trial",
                    refs.c.owner_id == trial["trial_id"],
                    refs.c.role == "hybrid",
                    refs.c.object_id == trial["intent"]["hybrid_ref"],
                )
            ).scalar_one()
            == trial["intent"]["hybrid_ref"]
        )
    report = history.objects.get_json(trial["result"]["replay_report_ref"], ObjectType.BLOB)
    assert report["mode"] == "hybrid" and report["actual"]["recorded_observations"] == 1
    assert trial["result"]["usage"]["tokens"] > 0 and trial["result"]["test_only"]
