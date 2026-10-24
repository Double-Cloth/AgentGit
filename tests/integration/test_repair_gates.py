"""S9 恶意补丁的关联审计、完整候选排序及终态墙钟冻结。"""

import hashlib
import json
import logging
import os
import time
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN
from test_repair_api import row_count
from test_repair_worker import create_case, drive

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.services.environment import source_revision
from agentgit.services.management import ManagementService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.validators import ValidatorService

pytestmark = pytest.mark.integration


def test_rejected_unsafe_patches_have_correlated_audit_and_zero_trial_execution(
    history, tmp_path, caplog, monkeypatch
):
    repo, search, research, session = create_case(history, tmp_path)
    manager = ManagementService(research)
    manifest = manager.repair_search(repo, search)["manifest"]
    original = manifest["space"]["patches"][0]["intervention"]
    attacks = []
    validator = deepcopy(original)
    validator["validator"] = {"rules": ["always_pass"]}
    attacks.append(("validator_override", "/interventions", validator, 422))
    permission = deepcopy(original)
    permission["payload"]["field"] = "agent_config.tools"
    attacks.append(("permission_escalation", "/interventions", permission, 422))
    path = deepcopy(original)
    path["payload"]["path"] = "../../host-file"
    attacks.append(("arbitrary_path", "/interventions", path, 422))
    cycle = deepcopy(original)
    cycle.update(
        operator="PLAN_PATCH",
        payload={
            "schema_version": 1,
            "kind": "PLAN_PATCH",
            "plan": {
                "schema_version": 1,
                "nodes": [
                    {
                        "schema_version": 1,
                        "key": "a",
                        "kind": "write_report",
                        "dependencies": ["b"],
                    },
                    {
                        "schema_version": 1,
                        "key": "b",
                        "kind": "write_sources",
                        "dependencies": ["a"],
                    },
                ],
            },
        },
    )
    attacks.append(("cyclic_plan", "/interventions", cycle, 422))
    confirm = ValidatorService(history).freeze(
        repo, session.state.goal, research.corpus, fixture_split="confirm"
    )
    protected = {
        "schema_version": 1,
        "failed_run_id": str(session.run_id),
        "base_commit": manifest["space"]["binding"]["base_commit"],
        "validator_ref": confirm,
        "targets": [{"schema_version": 1, "event_id": original["target_event_id"]}],
    }
    attacks.append(("confirm_oracle_in_screen", "/repair-searches", protected, 403))
    names = (
        "runs",
        "jobs",
        "repair_searches",
        "repair_trials",
        "repair_trial_jobs",
        "repair_trial_results",
    )
    before = {name: row_count(history, name) for name in names}
    evidence = []

    def forbidden(*args, **kwargs):
        pytest.fail("拒绝恶意补丁的请求不得调用模型或工具")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    with (
        caplog.at_level(logging.INFO, logger="agentgit.http"),
        TestClient(
            create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
        ) as client,
    ):
        for key, endpoint, body, status in attacks:
            response = client.post(endpoint, json=body, headers={"Idempotency-Key": key})
            assert response.status_code == status, response.text
            correlation = response.headers["X-Correlation-ID"]
            assert response.json()["correlation_id"] == correlation
            matching = [
                json.loads(record.message)
                for record in caplog.records
                if record.name == "agentgit.http"
                and json.loads(record.message).get("correlation_id") == correlation
            ]
            assert len(matching) == 1
            assert matching[0]["route"] == endpoint and matching[0]["status"] == status
            assert matching[0]["method"] == "POST"
            assert TOKEN not in json.dumps(matching) and "host-file" not in json.dumps(matching)
            assert {name: row_count(history, name) for name in names} == before
            evidence.append(
                dict(
                    case=key,
                    request_sha256=hashlib.sha256(encode(body)).hexdigest(),
                    response=response.json(),
                    log=matching[0],
                    counts=dict(before),
                )
            )
    assert RepairJobService(history).view(repo, search)["trials"] == []
    target = os.environ.get("AGENTGIT_S9_SAFETY_EVIDENCE")
    if target:
        with Path(target).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(
                    schema_version=1,
                    code_revision=source_revision(),
                    test_only=True,
                    unauthorized_calls=0,
                    created_trials=0,
                    cases=evidence,
                ),
                handle,
                ensure_ascii=False,
                indent=2,
            )


def test_immutable_comparison_explains_failed_alternatives_and_terminal_time_stops(
    history, tmp_path, monkeypatch
):
    repo, search, research, _ = create_case(history, tmp_path, mode="joint")
    result = drive(research, repo, search)
    rows = result["decision"]["comparison"]
    assert len(rows) == result["decision"]["explored_proposals"] == 4
    assert rows[0]["verdict"] == "passed" and rows[0]["patch_count"] == 2
    assert rows[0]["proposal_id"] == result["decision"]["selected_proposal_id"]
    assert all(row["verdict"] == "failed" and row["patch_count"] == 1 for row in rows[1:])
    assert all(row["wall_ms"] is not None and row["wall_ms"] >= 0 for row in rows)

    def readonly(*args, **kwargs):
        pytest.fail("终态查询不能写 CAS 或更新决定")

    monkeypatch.setattr(history.objects, "put", readonly)
    ledger = RepairJobService(history)
    before = ledger.view(repo, search)
    assert before["usage"]["elapsed_wall_ms"] == before["decision"]["usage"]["elapsed_wall_ms"]
    time.sleep(0.05)
    assert ledger.view(repo, search) == before


def test_cancelled_search_without_algorithm_decision_also_has_stable_terminal_time(
    history, tmp_path
):
    repo, search, _, _ = create_case(history, tmp_path)
    ledger = RepairJobService(history)
    ledger.cancel(repo, search, actor="operator")
    before = ledger.view(repo, search)
    assert before["status"] == "cancelled" and "decision" not in before
    time.sleep(0.05)
    assert ledger.view(repo, search) == before
