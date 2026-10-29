"""提案发现与共同祖先查询仅使用当前项目，不执行任务或重建候选。"""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from merge_proposal_cases import history_fingerprint
from merge_proposal_cases import merge_case as merge_case
from pydantic import SecretStr
from test_merge_proposal_api import TOKEN
from test_merge_proposals import expected, forbid_execution

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.core.errors import AgentGitError
from agentgit.core.merge_proposals import RejectMergeProposal

pytestmark = pytest.mark.integration


def test_discovery_is_bounded_read_only_and_project_scoped(history, merge_case, monkeypatch):
    repo, _, manager, request, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    first = service.create(repo, request, actor="operator", key="first")
    second = service.create(repo, request, actor="operator", key="second")
    service.reject(
        repo,
        UUID(first["proposal_id"]),
        RejectMergeProposal(**expected(first), reason="保留拒绝状态"),
        actor="operator",
        key="reject",
    )
    other = history.create_repository("不可借用的项目")
    before = history_fingerprint(history, repo)

    def forbidden(*args, **kwargs):
        pytest.fail("提案列表不能访问或发布 CAS")

    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        bases = client.get(
            "/merge-bases", params={"ours": request.ours.head, "theirs": request.theirs.head}
        )
        assert bases.status_code == 200, bases.text
        assert bases.json() == manager.merge_bases(repo, request.ours.head, request.theirs.head)
        assert (
            client.get(
                "/merge-bases", params={"ours": "../x", "theirs": request.theirs.head}
            ).status_code
            == 422
        )
        url = f"/repositories/{repo}/merge-proposals"
        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbidden)
            reading.setattr(history.objects, "get", forbidden)
            page = client.get(url, params={"limit": 1})
            assert page.status_code == 200, page.text
            page = page.json()
            assert page["items"][0]["proposal_id"] == second["proposal_id"]
            assert page["items"][0]["status"] == "open"
            assert page["next_after_id"] == second["proposal_id"]
            last = client.get(url, params={"after_id": page["next_after_id"], "limit": 1}).json()
            assert last["next_after_id"] is None
            assert last["items"][0]["proposal_id"] == first["proposal_id"]
            assert last["items"][0]["status"] == "rejected"
            assert client.get(url, params={"after_id": str(uuid4())}).status_code == 404
            assert client.get(url, headers={"X-Repository-ID": str(other)}).status_code == 404
            foreign = client.get(
                f"/repositories/{other}/merge-proposals",
                headers={"X-Repository-ID": str(other)},
                params={"after_id": first["proposal_id"]},
            )
            assert foreign.status_code == 404
            for limit in (0, 101, "true"):
                assert client.get(url, params={"limit": limit}).status_code == 422
            for limit in (False, 0, 101):
                with pytest.raises(AgentGitError):
                    manager.merge_proposals_list(repo, limit=limit)
            assert "semantic_merge" in client.get("/capabilities").json()["features"]
            assert "merged_run_initialization" in client.get("/capabilities").json()["features"]
    assert history_fingerprint(history, repo) == before
