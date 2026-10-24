"""公共观测模板与自动控制器的 exact Hybrid 通道，保留失败和 synthetic 范围。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_recorded_replayer import replay_source as replay_source

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.research import BudgetLimits, evolve
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.management import ManagementService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def test_public_observation_template_requires_hybrid_and_cannot_hide_original_failure(
    history, replay_source
):
    session, _, commits, _ = replay_source
    repo = session.repository_id
    manager = ManagementService(session.service)
    validator = ValidatorService(history).freeze(repo, session.state.goal, session.service.corpus)
    bad = evolve(
        session.state.plan,
        nodes=tuple(n for n in session.state.plan.nodes if n.kind != "write_sources"),
    )
    created = manager.create_experiment(
        repo,
        NewExperiment(
            source_run_id=str(session.run_id),
            from_commit=commits[3],
            validator_ref=validator,
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="正常"),
                ExperimentGroup(key="B", label="缺来源文件", plan=bad),
            ),
        ),
        "observation-source",
    )
    worker = Worker(manager.jobs)
    assert sorted(worker.run_once()["status"] for _ in range(2)) == ["completed", "failed"]
    failed = next(
        t
        for t in manager.experiment(repo, UUID(created["experiment_id"]))["trials"]
        if t["group"] == "B"
    )
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == UUID(failed["run_id"]),
                    TABLES["external_calls"].c.logical_step_id == "read-5",
                )
            )
            .mappings()
            .one()
        )
        event = connection.execute(
            sa.select(TABLES["events"].c.id).where(
                TABLES["events"].c.run_id == UUID(failed["run_id"]),
                TABLES["events"].c.logical_step_id == "read-5",
                TABLES["events"].c.type == "tool.started",
            )
        ).scalar_one()
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    body = {
        "schema_version": 1,
        "failed_run_id": failed["run_id"],
        "base_commit": intent.checkpoint,
        "validator_ref": validator,
        "targets": [
            {"schema_version": 1, "event_id": event, "replacement_call_ids": [str(call["id"])]}
        ],
    }
    with pytest.raises(AgentGitError) as error:
        manager.create_repair_search(
            repo, NewRepairSearch.model_validate_json(encode(body)), "missing-hybrid"
        )
    assert error.value.code == ErrorCode.FORBIDDEN and worker.run_once() is None
    body["hybrid"] = {
        "schema_version": 1,
        "until_commit": failed["checkpoint_commit_id"],
        "calls": [str(call["id"])],
    }
    submitted = manager.create_repair_search(
        repo, NewRepairSearch.model_validate_json(encode(body)), "exact-observation"
    )
    runner = RepairSearchWorker(session.service)
    for _ in range(8):
        result = runner.run_once(repository=repo)
        assert result["search_id"] == submitted["search_id"]
        if result["status"] == "no_solution_in_space":
            break
        assert result["status"] == "running"
        assert worker.run_once() is not None
    else:
        pytest.fail("自动观测搜索没有按声明空间终止")
    assert result["decision"]["total_proposals"] == result["decision"]["determinate_failures"] == 1
    trial = result["trials"][-1]["result"]
    report = history.objects.get_json(trial["replay_report_ref"], ObjectType.BLOB)
    assert trial["test_only"] and trial["verdict"] == "failed"
    assert report["mode"] == "hybrid" and report["actual"]["synthetic_observations"] == 1
    assert report["actual"]["tool_calls"] == trial["usage"]["tool_calls"]
    assert worker.run_once() is None
