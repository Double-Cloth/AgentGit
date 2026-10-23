"""实验发现只读元数据，不依赖当前代码重新验收旧 manifest。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_experiment_entrypoints import client_for
from test_recorded_replayer import replay_source as replay_source

from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.research import BudgetLimits
from agentgit.services.management import ManagementService
from agentgit.storage.schema import build_metadata

pytestmark = pytest.mark.integration
TABLES = build_metadata().tables


def test_listing_pages_project_metadata_without_regrading(history, replay_source, monkeypatch):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    repo = session.repository_id
    with client_for(manager, repo) as client:
        frozen = client.post(
            "/validators",
            json={
                "schema_version": 1,
                "source_run_id": str(session.run_id),
                "from_commit": commits[15],
            },
            headers={"Idempotency-Key": "listing-freeze"},
        )
        assert frozen.status_code == 201, frozen.text
        ref = frozen.json()["validator_ref"]
    request = NewExperiment(
        source_run_id=str(session.run_id),
        from_commit=commits[15],
        validator_ref=ref,
        repeats=1,
        budget=BudgetLimits(),
        groups=(ExperimentGroup(key="A", label="A"), ExperimentGroup(key="B", label="B")),
    )
    with client_for(manager, repo) as client:
        ids = [
            client.post(
                "/experiments",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": f"listing-{index}"},
            ).json()["experiment_id"]
            for index in range(3)
        ]

        def forbidden(*args, **kwargs):
            raise AssertionError("列表不得读取验收器或实验 manifest")

        monkeypatch.setattr(manager.experiments.validators, "manifest", forbidden)
        monkeypatch.setattr(manager.experiments, "manifest", forbidden)
        endpoint = f"/repositories/{repo}/experiments"
        first = client.get(endpoint, params={"limit": 2})
        assert first.status_code == 200, first.text
        data = first.json()
        assert [item["id"] for item in data["items"]] == sorted(ids)[:2]
        assert data["has_more"] and data["next_after_id"] == sorted(ids)[1]
        assert all(item["trial_count"] == 2 for item in data["items"])
        assert all(item["validator_ref"] == ref for item in data["items"])
        last = client.get(endpoint, params={"limit": 2, "after_id": data["next_after_id"]}).json()
        assert [item["id"] for item in last["items"]] == sorted(ids)[2:]
        assert not last["has_more"] and last["next_after_id"] is None
        other = history.create_repository("列表隔离项目")
        assert client.get(endpoint, headers={"X-Repository-ID": str(other)}).status_code == 404
        assert (
            client.get(
                f"/repositories/{other}/experiments", headers={"X-Repository-ID": str(other)}
            ).json()["items"]
            == []
        )
        assert client.get(endpoint, headers={"Authorization": "Bearer wrong"}).status_code == 401
        for params in ({"limit": 0}, {"limit": 101}, {"after_id": "bad"}):
            assert client.get(endpoint, params=params).status_code == 422
        features = client.get("/capabilities").json()["features"]
        assert {
            "controlled_experiments",
            "hybrid_observations",
            "typed_interventions",
            "experiment_statistics",
        } <= set(features)
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["jobs"])
                ).scalar_one()
                == 6
            )
        assert UUID(ids[0])
