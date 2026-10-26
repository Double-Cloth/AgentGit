"""搜索与确认池原子发布，实际并发领取、回滚、单连接池和后续六加四。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from fastapi.testclient import TestClient
from pydantic import SecretStr
from repair_case_execution import drive as drive_phase
from repair_case_execution import reduce_and_open, worker
from test_management_api import TOKEN
from test_repair_api import public_request
from test_repair_pools import setup_pool
from test_repair_worker import ConditionalMock, drive
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.models import parse_contract
from agentgit.core.repair_pools import NewPreparedRepairSearch
from agentgit.core.research import ModelRequest
from agentgit.services.history import HistoryService
from agentgit.services.management import ManagementService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_pools import TABLES, RepairValidationPoolService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration


def binding():
    return "prepared-fixture-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class PreparedSearchModel(MockResearchModel):
    """固定源故障保留规则；其他任务使用标准 Mock，不读取验证阶段或结果。"""

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        if request.state.goal.text.startswith("prompt;"):
            assert binding() in request.state.goal.text
            return ConditionalMock().invoke(payload)
        return super().invoke(payload)


def prepared_case(history, tmp_path):
    repo, old, research, _, suite = setup_pool(history, tmp_path, adapter_binding=binding())
    body = dict(
        schema_version=1,
        search=public_request(history, repo, old),
        regression=[origin.model_dump(mode="json") for origin in suite.regression],
    )
    return repo, research, NewPreparedRepairSearch.model_validate_json(encode(body))


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in (
                "repair_searches",
                "repair_jobs",
                "repair_proposals",
                "repair_validation_suites",
                "repair_validation_samples",
                "jobs",
                "runs",
            )
        }


def test_http_publish_blocks_early_claim_then_actual_screen_reduce_and_six_plus_four(
    history, tmp_path, monkeypatch
):
    repo, research, request = prepared_case(history, tmp_path)
    manager = ManagementService(research)
    before = counts(history)
    entered, resume = Event(), Event()
    original = RepairValidationPoolService.publish

    def pause(self, *args, **kwargs):
        entered.set()
        assert resume.wait(20), "测试等待领取观察超时"
        return original(self, *args, **kwargs)

    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        with monkeypatch.context() as publication, ThreadPoolExecutor(max_workers=1) as executor:
            publication.setattr(RepairValidationPoolService, "publish", pause)
            future = executor.submit(
                client.post,
                "/repair-searches/prepared",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": "prepared-http"},
            )
            try:
                assert entered.wait(20)
                assert RepairJobService(history).claim("early-worker", repository=repo) is None
                assert counts(history) == before
            finally:
                resume.set()
            response = future.result(timeout=20)
        assert response.status_code == 202, response.text
        made = response.json()
        search, suite = UUID(made["search_id"]), made["validation_suite"]
        assert suite["search_id"] == str(search) and suite["response_status"] == "not_generated"
        assert counts(history)["repair_validation_samples"] == 10
        assert (
            counts(history)["jobs"] == before["jobs"] and counts(history)["runs"] == before["runs"]
        )
        assert "prepared_repair_search" in client.get("/capabilities").json()["features"]
        assert (
            client.post(
                "/repair-searches/prepared",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": "prepared-http"},
            ).json()
            == made
        )
        changed = request.model_dump(mode="json")
        changed["regression"][0]["from_commit"] = "e" * 64
        assert (
            client.post(
                "/repair-searches/prepared",
                json=changed,
                headers={"Idempotency-Key": "prepared-http"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/repair-searches/prepared",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": "foreign", "X-Repository-ID": str(uuid4())},
            ).status_code
            == 404
        )
    ledger = RepairJobService(history)
    lease = ledger.claim("after-commit", repository=repo)
    assert lease is not None and lease.search_id == str(search)
    ledger.release(lease)
    source = drive(research, repo, search)
    assert source["status"] == "needs_confirmation"
    actual = worker(research, PreparedSearchModel)
    study, reduced = reduce_and_open(research, repo, search, suite, actual, field_policy=None)
    result = drive_phase(research, repo, study, actual, "confirm")
    assert result["status"] == "verified_under_fixture", result
    assert result["verification"]["confirm_passed"] == 6
    assert result["verification"]["regression_passed"] == 4
    assert manager.create_prepared_repair_search(repo, request, "prepared-http") == made
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-prepared-search-atomic-publication/v1",
        environment="AGENTGIT_S10_PREPARED_SEARCH_EVIDENCE",
    )


def test_full_registration_rollback_leaves_no_claimable_search_and_retry_is_exact(
    history, tmp_path, monkeypatch
):
    repo, research, request = prepared_case(history, tmp_path)
    manager = ManagementService(research)
    before = counts(history)
    original = RepairValidationPoolService.publish

    def rollback(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("registration-written-before-outer-commit")

    with monkeypatch.context() as fault:
        fault.setattr(RepairValidationPoolService, "publish", rollback)
        with pytest.raises(RuntimeError, match="registration-written"):
            manager.create_prepared_repair_search(repo, request, "rollback")
    assert counts(history) == before
    assert RepairJobService(history).claim("after-rollback", repository=repo) is None
    made = manager.create_prepared_repair_search(repo, request, "rollback")
    committed = counts(history)

    def forbidden(*args, **kwargs):
        pytest.fail("原请求重试不能重新生成模板、写 CAS 或调用模型工具")

    with monkeypatch.context() as retry:
        retry.setattr(RepairSearchService, "compile", forbidden)
        retry.setattr(history.objects, "put", forbidden)
        retry.setattr(MockResearchModel, "invoke", forbidden)
        retry.setattr(ToolRegistry, "invoke", forbidden)
        assert manager.create_prepared_repair_search(repo, request, "rollback") == made
    assert counts(history) == committed
    assert committed["repair_searches"] == before["repair_searches"] + 1
    assert committed["repair_validation_suites"] == 1
    assert committed["repair_validation_samples"] == 10


def test_six_prepared_submissions_in_single_connection_pool_and_cli_share_one_identity(
    history, tmp_path, monkeypatch
):
    repo, research, request = prepared_case(history, tmp_path)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    engine = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    manager = ManagementService(
        ResearchService(HistoryService(engine, history.objects), research.data_root)
    )
    before = counts(history)

    def forbidden(*args, **kwargs):
        pytest.fail("准备与重试不能执行模型或工具")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(
                executor.map(
                    lambda _: manager.create_prepared_repair_search(repo, request, "six"), range(6)
                )
            )
        assert all(result == results[0] for result in results)
        after = counts(history)
        assert after["repair_searches"] == before["repair_searches"] + 1
        assert after["repair_validation_suites"] == 1 and after["repair_validation_samples"] == 10
        assert after["jobs"] == before["jobs"] and after["runs"] == before["runs"]

        @contextmanager
        def current(testing):
            yield manager

        monkeypatch.setattr("agentgit.cli.main.management", current)
        path = tmp_path / "prepared.json"
        path.write_bytes(encode(request.model_dump(mode="json")))
        reply = CliRunner().invoke(
            app, ["repair-prepare", str(repo), "--file", str(path), "--key", "six"]
        )
        assert reply.exit_code == 0, reply.output
        assert json.loads(reply.output) == results[0]
        assert counts(history) == after
    finally:
        engine.dispose()
