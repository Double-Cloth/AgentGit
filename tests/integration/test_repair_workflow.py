"""真实完整修复流程只读发现；重开查询不依赖 POST 或写 CAS。"""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN
from test_repair_confirmation import drive_confirmation, setup_confirmation

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.services.management import ManagementService
from agentgit.services.repair_reviews import RepairReviewService

pytestmark = pytest.mark.integration


def test_readonly_workflow_discovers_only_own_registered_study_and_final_proposal(
    history, tmp_path, monkeypatch
):
    repo, study, research, source, _ = setup_confirmation(history, tmp_path)
    manager = ManagementService(research)
    search = UUID(source["search_id"])
    initial = manager.repair_workflow(repo, search)
    assert initial["study_id"] == str(study) and initial["suite_id"] is not None
    assert initial["proposal_id"] is None
    confirmed = drive_confirmation(history, research, repo)
    assert confirmed["status"] == "verified_under_fixture"
    made = RepairReviewService(research).create(repo, study, actor="operator", key="proposal")

    def forbidden(*args, **kwargs):
        pytest.fail("重开查询不得写入 CAS 或重新提交动作")

    monkeypatch.setattr(history.objects, "put", forbidden)
    monkeypatch.setattr(manager, "create_repair_study", forbidden)
    monkeypatch.setattr(manager, "create_repair_proposal", forbidden)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        for _ in range(2):
            result = client.get(f"/repair-searches/{search}/workflow")
            assert result.status_code == 200
            assert result.json() == {**initial, "proposal_id": made["proposal_id"]}
        assert (
            client.get(
                f"/repair-searches/{search}/workflow", headers={"X-Repository-ID": str(uuid4())}
            ).status_code
            == 404
        )
