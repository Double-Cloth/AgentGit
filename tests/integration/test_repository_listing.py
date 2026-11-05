"""项目列表只读分页：新项目在前、游标稳定、未授权访问被拒。"""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration
TOKEN = "fixture-repository-listing-token-0000000000"


def client_for(history, tmp_path, *, authorized=True):
    app = create_app(
        Settings(api_token=SecretStr(TOKEN)),
        management=ManagementService(ResearchService(history, tmp_path / "listing")),
    )
    headers = {"Authorization": "Bearer " + TOKEN} if authorized else {}
    return TestClient(app, base_url="http://127.0.0.1", headers=headers)


def test_repository_listing_pages_newest_first_with_stable_cursor(history, tmp_path):
    first = history.create_repository("列表较早项目")
    second = history.create_repository("列表较新项目")
    with client_for(history, tmp_path) as client:
        assert "repository_listing" in client.get("/capabilities").json()["features"]
        page = client.get("/repositories", params={"limit": 1}).json()
        assert page["items"][0]["id"] == str(second)
        assert page["items"][0]["name"] == "列表较新项目"
        assert page["items"][0]["created_at"] and set(page["items"][0]) == {
            "id",
            "name",
            "created_at",
        }
        assert page["next_after_id"] == str(second)
        rest = client.get(
            "/repositories", params={"limit": 1, "after_id": page["next_after_id"]}
        ).json()
        assert rest["items"][0]["id"] == str(first)
        assert rest["next_after_id"] is None

        full = client.get("/repositories", params={"limit": 100}).json()
        ids = [item["id"] for item in full["items"]]
        assert str(first) in ids and str(second) in ids and len(ids) == len(set(ids))


def test_repository_listing_rejects_bad_cursor_and_missing_identity(history, tmp_path):
    history.create_repository("列表守卫项目")
    with client_for(history, tmp_path) as client:
        assert (
            client.get(
                "/repositories", params={"after_id": "00000000-0000-4000-8000-000000000000"}
            ).status_code
            == 404
        )
        assert client.get("/repositories", params={"limit": 0}).status_code == 422
        assert client.get("/repositories", params={"limit": 101}).status_code == 422
    with client_for(history, tmp_path, authorized=False) as anonymous:
        assert anonymous.get("/repositories").status_code == 401
