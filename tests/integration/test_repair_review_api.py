"""HTTP/CLI 共用精确审阅，保存实验只落定义且只读查询不写 CAS。"""

from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN
from test_repair_adoption import counts
from test_repair_review_controls import confirmed
from test_repair_worker import ConditionalMock
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.repair_reviews import DecideRepairReview
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.repair_reviews import RepairReviewService

pytestmark = pytest.mark.integration


def test_http_and_cli_exact_review_inputs_and_save_experiment_have_no_execution(
    history, tmp_path, monkeypatch
):
    repo, _, research, _, identity, proposal, _ = confirmed(history, tmp_path)
    manager = ManagementService(research)
    headers = {"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)}
    before = counts(history)

    def forbidden(*args, **kwargs):
        pytest.fail("审阅和保存定义不能调用模型、工具、提交 Job 或在查询时写 CAS")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ConditionalMock, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    monkeypatch.setattr(JobService, "enqueue", forbidden)

    @contextmanager
    def management(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", management)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers=headers,
    ) as client:
        url = f"/repair-proposals/{identity}"
        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbidden)
            assert client.get(url).json() == manager.repair_proposal(repo, identity)
            assert client.get(url, headers={"X-Repository-ID": str(uuid4())}).status_code == 404
            cli = CliRunner().invoke(app, ["repair-proposal-status", str(repo), str(identity)])
            assert cli.exit_code == 0, cli.output
        key = {"Idempotency-Key": "api-review"}
        for body, status in (
            ({}, 409),
            ({"schema_version": 1, "passed": True}, 422),
            ({"schema_version": 2}, 409),
        ):
            assert client.post(url + "/review", json=body, headers=key).status_code == status
        opened = client.post(url + "/review", json={"schema_version": 1}, headers=key)
        assert opened.status_code == 201, opened.text
        ticket = opened.json()
        assert (
            client.post(url + "/review", json={"schema_version": 1}, headers=key).json() == ticket
        )
        request = DecideRepairReview(
            review_id=ticket["review_id"],
            proposal_ref=proposal["proposal_ref"],
            expected_config_version=ticket["ticket"]["expected_config_version"],
            decision="save_experiment",
        )
        for changed in (
            {**request.model_dump(mode="json"), "after": {}},
            {**request.model_dump(mode="json"), "decision": "reject"},
        ):
            assert (
                client.post(
                    url + "/approve", json=changed, headers={"Idempotency-Key": "bad"}
                ).status_code
                == 422
            )
        assert (
            client.post(
                url + "/approve",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": "bad-auth", "Authorization": "Bearer invalid"},
            ).status_code
            == 401
        )

        def crash(point):
            if point == "before_decision_commit":
                raise RuntimeError("保存定义与审阅决定提交前中断")

        with pytest.raises(RuntimeError):
            RepairReviewService(research, fault=crash).decide(
                repo, identity, request, actor=manager.actor, key="saved-via-cli"
            )
        assert counts(history) == before
        file = tmp_path / "review.json"
        file.write_bytes(encode(request.model_dump(mode="json")))
        args = [
            "repair-review-decision",
            str(repo),
            str(identity),
            "--file",
            str(file),
            "--key",
            "saved-via-cli",
        ]
        saved = CliRunner().invoke(app, args)
        assert saved.exit_code == 0, saved.output
        assert CliRunner().invoke(app, args).output == saved.output
        after = counts(history)
        assert after == {**before, "proposal_reviews": 1, "repair_saved_experiments": 1}
        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbidden)
            fetched = client.get(url)
            assert fetched.status_code == 200, fetched.text
            review = fetched.json()["review"]
            definition = review["saved_experiment"]["definition"]
            assert definition["execution"] == "not_started" and definition["test_only"]
            assert definition["patches"] == proposal["proposal"]["patches"]
            assert review["decision"]["new_config_id"] is None
            assert UUID(review["decision"]["saved_experiment_id"])
