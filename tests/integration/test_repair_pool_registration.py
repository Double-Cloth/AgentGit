"""预注册的并发发布、准备期间竞争和可执行起点边界。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_repair_pools import counts, setup_pool

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_pools import NewRepairValidationSuite
from agentgit.core.research import ResearchGoal
from agentgit.services.history import HistoryService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration


def test_six_single_pool_registrations_publish_one_without_model_or_tool(
    history, tmp_path, monkeypatch
):
    repo, _, research, _, request = setup_pool(history, tmp_path)
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
    pools = RepairValidationPoolService(
        ResearchService(HistoryService(engine, history.objects), research.data_root)
    )
    before = counts(history)

    def forbidden(*args, **kwargs):
        pytest.fail("预注册不得执行模型或工具")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(
                executor.map(
                    lambda _: pools.submit(repo, request, actor="operator", key="six"), range(6)
                )
            )
        assert all(value == results[0] for value in results)
        assert counts(history)["repair_validation_samples"] == 10
        assert counts(history)["repair_validation_suites"] == 1
        assert (
            counts(history)["jobs"] == before["jobs"] and counts(history)["runs"] == before["runs"]
        )
    finally:
        engine.dispose()


def test_first_claim_during_preparation_prevents_registration(history, tmp_path, monkeypatch):
    repo, _, _, pools, request = setup_pool(history, tmp_path)
    original = pools._origin
    claimed = []

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        if not claimed:
            claimed.append(pools.screening.claim("racing-worker", repository=repo))
            assert claimed[0] is not None
        return result

    monkeypatch.setattr(pools, "_origin", prepare)
    with pytest.raises(AgentGitError) as error:
        pools.submit(repo, request, actor="operator", key="late-preparation")
    assert error.value.code == ErrorCode.FORBIDDEN
    assert (
        counts(history)["repair_validation_suites"]
        == counts(history)["repair_validation_samples"]
        == 0
    )


def test_completed_output_and_foreign_run_cannot_be_registered_as_regression_origin(
    history, tmp_path
):
    repo, _, research, pools, request = setup_pool(history, tmp_path)
    body = request.model_dump(mode="json")
    with history.engine.connect() as connection:
        run = history._run(connection, repo, UUID(body["regression"][0]["source_run_id"]))
        body["regression"][0]["from_commit"] = run["checkpoint_commit_id"]
    with pytest.raises(AgentGitError) as error:
        pools.submit(
            repo,
            NewRepairValidationSuite.model_validate_json(encode(body)),
            actor="operator",
            key="terminal",
        )
    assert error.value.code == ErrorCode.FORBIDDEN
    other = history.create_repository("其他项目")
    task = research.create_task(
        other, research.create_config(other), ResearchGoal(text="其他项目固定任务")
    )
    session = research.start(other, task)
    session.execute()
    body["regression"][0]["source_run_id"] = str(session.run_id)
    with pytest.raises(AgentGitError) as error:
        pools.submit(
            repo,
            NewRepairValidationSuite.model_validate_json(encode(body)),
            actor="operator",
            key="foreign",
        )
    assert error.value.code == ErrorCode.NOT_FOUND
    assert counts(history)["repair_validation_suites"] == 0
