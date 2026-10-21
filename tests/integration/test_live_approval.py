"""Live 即使重用相同参数及效果策略，也必须获得新 Run 的精确审批。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_approvals import effect_server as effect_server
from test_reconciliation import EffectfulWorker, confirm
from test_reconciliation import unknown as unknown

from agentgit.core.canonical import ObjectType
from agentgit.core.effects import BoundAction
from agentgit.core.models import parse_contract
from agentgit.core.versions import LiveReplay
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


def test_live_does_not_inherit_source_approval_or_send_before_new_authorization(history, unknown):
    queue, repo, source_run, _, source_call, worker, counts, adapter = unknown
    confirm(unknown)
    result = worker().run_once()
    assert result["status"] == "completed", result
    source_run = UUID(result["run_id"])
    original = history.branch(repo)
    before = counts()
    chain = VersionService(history).log(repo)["items"]
    checkpoint = next(
        item["commit_id"]
        for item in reversed(chain)
        if (history.restore(repo, item["commit_id"]).data.get("pending_tool") or {}).get("tool")
        == "corpus_search"
    )
    manager = ManagementService(queue.research)
    response = manager.replay(
        repo,
        LiveReplay(
            source_run_id=str(source_run), from_commit=checkpoint, branch_name="fresh-approval"
        ),
        "fresh-approval",
    )
    job_id, run_id = UUID(response["job_id"]), UUID(response["run_id"])
    namespace = worker().resolver.namespace
    assert (
        EffectfulWorker(manager.jobs, adapter.base_url, namespace).run_once()["status"]
        == "waiting_approval"
    )
    assert counts() == before
    with history.engine.connect() as connection:
        approvals = list(
            connection.execute(
                sa.select(TABLES["approvals"]).where(TABLES["approvals"].c.run_id == run_id)
            ).mappings()
        )
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == run_id,
                    TABLES["external_calls"].c.provider == "tool:corpus_search/v1",
                )
            ).mappings()
        )
    assert len(approvals) == 1 and calls == []
    assert approvals[0]["decision"] is None
    action = parse_contract(
        history.objects.get(approvals[0]["action_ref"], ObjectType.CALL), BoundAction
    )
    assert action.run_id == str(run_id)
    assert action.occurrence.request_digest == source_call["request_digest"]
    assert history.branch(repo) == original
    assert manager.replay_status(repo, job_id)["replay"]["actual"]["tool_calls"] == 0
