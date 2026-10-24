"""原生产 Mock 与公共默认预算的自动 Plan 修复；不注入条件模型替身。"""

import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from diagnosis_evidence import pack_repository_evidence
from test_debug_candidate_experiments import candidate_case as candidate_case
from test_recorded_replayer import replay_source as replay_source
from test_repair_ledger import repair_case as repair_case

from agentgit.core.canonical import encode
from agentgit.core.repair import NewRepairSearch
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_original_mock_public_default_budget_finds_plan_repair(
    history, repair_case, candidate_case, tmp_path
):
    repo, space, _ = repair_case
    research = ResearchService(history, tmp_path / "ordinary-mock")
    request = NewRepairSearch.model_validate_json(
        encode(
            {
                "schema_version": 1,
                "failed_run_id": space.binding.failed_run_id,
                "base_commit": space.binding.base_commit,
                "validator_ref": space.binding.validator_ref,
                "targets": [
                    {"schema_version": 1, "event_id": space.patches[0].intervention.target_event_id}
                ],
            }
        )
    )
    manager = ManagementService(research)
    created = manager.create_repair_search(repo, request, "ordinary-mock-default")
    search = UUID(created["search_id"])
    original = history.branch(repo)
    runner, worker = RepairSearchWorker(research), Worker(JobService(research))
    for _ in range(20):
        result = runner.run_once(repository=repo)
        assert result is not None
        assert result["search_id"] == str(search)
        if result["status"] == "needs_confirmation":
            break
        assert result["status"] == "running"
        assert worker.run_once() is not None
    else:
        pytest.fail("生产 Mock 的默认自动修复没有找到候选")
    decision = result["decision"]
    assert decision["explored_proposals"] == 3 and decision["unexplored_proposals"] == 4
    assert decision["passed_proposals"] == 1 and decision["determinate_failures"] == 2
    assert decision["confirmation"] == "not_run" and decision["usage"]["active"] == 0
    assert result["manifest"]["budget"]["trial"]["max_tokens"] == 200_000
    assert result["manifest"]["budget"]["max_tokens"] == 3_000_000
    assert result["trials"][-1]["result"]["usage"]["model_calls"] == 3
    assert result["trials"][-1]["result"]["usage"]["tool_calls"] == 2
    assert history.branch(repo) == original and worker.run_once() is None
    folder = os.environ.get("AGENTGIT_S9_SEARCH_EVIDENCE")
    if folder:
        reference = candidate_case[0].run_id
        failed = UUID(space.binding.failed_run_id)
        evidence = dict(
            repository_id=str(repo),
            failed_run_id=str(failed),
            reference_run_id=str(reference),
            case_id="S9-production-mock-default/v1",
            graph=BehaviorService(history).trace(repo, failed).model_dump(mode="json"),
            reference_graph=BehaviorService(history).trace(repo, reference).model_dump(mode="json"),
        )
        raw = pack_repository_evidence(history, evidence)
        path = Path(folder) / "production-mock.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(
                    schema_version=1,
                    test_only=True,
                    model_adapter="MockResearchModel",
                    model_override=False,
                    search=result,
                    request=request.model_dump(mode="json"),
                    evidence=evidence,
                    raw_repository=raw,
                ),
                handle,
                ensure_ascii=False,
                indent=2,
            )
