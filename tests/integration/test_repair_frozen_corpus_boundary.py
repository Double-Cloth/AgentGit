"""无冻结 Job 的旧自定义语料来源不能静默回退到 bundled 数据。"""

import pytest
import sqlalchemy as sa
from test_repair_confirmation_variants import regression_corpora

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import AgentConfig, ResearchGoal
from agentgit.core.versions import LiveReplay
from agentgit.services.repair_confirmation import TABLES
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("entry", ["replay", "validation_pool"])
def test_legacy_custom_corpus_requires_consistent_frozen_inputs(history, tmp_path, entry):
    corpus = regression_corpora()[1]
    service = ResearchService(history, tmp_path / "legacy-custom", corpus=corpus)
    repo = history.create_repository("自定义旧语料边界")
    task = service.create_task(
        repo, service.create_config(repo), ResearchGoal(text="独立自定义事实任务")
    )
    session = service.start(repo, task)
    assert session.execute().status.value == "completed"
    with history.engine.connect() as connection:
        links = TABLES["run_commits"]
        first = connection.execute(
            sa.select(links.c.commit_id)
            .where(links.c.run_id == session.run_id)
            .order_by(links.c.run_order)
            .limit(1)
        ).scalar_one()
    with pytest.raises(AgentGitError) as error:
        if entry == "replay":
            ReplayService(service).prepare(
                repo,
                LiveReplay(
                    source_run_id=str(session.run_id),
                    from_commit=first,
                    branch_name="reject-corpus-swap",
                ),
            )
        else:
            RepairValidationPoolService(service)._origin(
                repo, str(session.run_id), first, "regression", AgentConfig(), "live"
            )
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
