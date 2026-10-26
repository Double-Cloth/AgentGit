"""Hybrid 验证仍重新调用模型，历史只读工具观测与实际调用分别计量。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from test_repair_confirmation import ConfirmationMock, adapter_binding, drive_confirmation
from test_repair_studies import drive_study
from test_repair_worker import create_case, drive

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.repair_pools import NewRepairValidationSuite
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ModelRequest, ResearchGoal
from agentgit.services.management import ManagementService
from agentgit.services.repair_confirmation import TABLES
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_studies import RepairStudyService

pytestmark = pytest.mark.integration


def test_hybrid_six_confirmations_own_policy_and_fresh_model_calls_then_live_regressions(
    history, tmp_path
):
    repo, old, research, failed = create_case(
        history, tmp_path, adapter_binding=adapter_binding("pass")
    )
    old_source = RepairJobService(history).view(repo, old)["manifest"]
    RepairJobService(history).cancel(repo, old, actor="fixture")
    with history.engine.connect() as connection:
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == failed.run_id
                )
            ).mappings()
        )
        selected = next(c for c in calls if c["logical_step_id"] == "read-5")
        for call in calls:
            intent = parse_contract(
                history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
            )
            if not intent.occurrence.provider.startswith("llm:"):
                continue
            request = parse_contract(
                history.objects.get(intent.request_ref, ObjectType.BLOB), ModelRequest
            )
            ready = request.state.plan.ready(request.state.completed) if request.state.plan else ()
            if request.phase == "act" and ready and ready[0].key == "read-5":
                event = connection.execute(
                    sa.select(TABLES["events"].c.id).where(
                        TABLES["events"].c.run_id == failed.run_id,
                        TABLES["events"].c.logical_step_id == call["logical_step_id"],
                        TABLES["events"].c.type == "llm.requested",
                    )
                ).scalar_one()
                base = intent.checkpoint
                break
        else:
            pytest.fail("未找到 Hybrid 只读观测之前的模型起点")
    made = ManagementService(research).create_repair_search(
        repo,
        NewRepairSearch.model_validate_json(
            encode(
                dict(
                    schema_version=1,
                    failed_run_id=str(failed.run_id),
                    base_commit=base,
                    validator_ref=old_source["space"]["binding"]["validator_ref"],
                    targets=[dict(schema_version=1, event_id=event)],
                    hybrid=dict(
                        schema_version=1, until_commit=failed.head, calls=[str(selected["id"])]
                    ),
                )
            )
        ),
        "hybrid-search",
    )
    search = UUID(made["search_id"])
    origins, config = [], research.create_config(repo)
    for index in range(4):
        task = research.create_task(
            repo,
            config,
            ResearchGoal(text=f"regression-{index};{adapter_binding('pass')};Hybrid 固定回归任务"),
        )
        run = research.start(repo, task)
        assert run.execute(ConfirmationMock()).status.value == "completed"
        with history.engine.connect() as connection:
            links = TABLES["run_commits"]
            first = connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == run.run_id)
                .order_by(links.c.run_order)
                .limit(1)
            ).scalar_one()
        origins.append(dict(schema_version=1, source_run_id=str(run.run_id), from_commit=first))
    pools = RepairValidationPoolService(research)
    suite = pools.submit(
        repo,
        NewRepairValidationSuite.model_validate_json(
            encode(dict(schema_version=1, search_id=str(search), regression=origins))
        ),
        actor="operator",
        key="suite",
    )
    source = drive(research, repo, search)
    assert source["status"] == "needs_confirmation"
    made = RepairStudyService(history).submit(
        repo, NewRepairStudy(search_id=str(search)), actor="operator", key="study"
    )
    study = UUID(made["study_id"])
    reduced = drive_study(research, repo)
    assert pools.open(repo, study, UUID(suite["suite_id"]), actor="operator", key="open")["opened"]
    result = drive_confirmation(history, research, repo)
    assert result["status"] == "verified_under_fixture", result
    for trial in result["trials"]:
        receipt = trial["result"]
        report = history.objects.get_json(receipt["replay_report_ref"], ObjectType.BLOB)
        if receipt["phase"] == "confirm":
            assert report["mode"] == "hybrid" and report["actual"]["recorded_observations"] == 1
            assert report["new_usage"]["tool_calls"] == receipt["usage"]["tool_calls"] + 1
            assert trial["projection"]["hybrid_ref"]
        else:
            assert report["mode"] == "live" and trial["projection"]["hybrid_ref"] is None
        assert receipt["fresh_model_calls"] > 0 and receipt["test_only"]
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-hybrid-confirm-live-regression/v1",
        environment="AGENTGIT_S10_HYBRID_CONFIRMATION_EVIDENCE",
    )
