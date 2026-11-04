"""固定回归和只读工具环境经真实 Worker/Run；不使用保留案例或修复成绩。"""

import json
import os
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from diagnosis_evidence import pack_repository_evidence
from repair_benchmark_environment import ObservationWorker
from repair_benchmark_origins import select_origins
from repair_benchmark_protocol import method_plan
from repair_benchmark_regressions import regression_inputs
from repair_fault_runtime import FaultCondition, FaultResearchModel

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairBinding
from agentgit.core.research import AgentConfig
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.policy import mock_budget, mock_policy_version
from agentgit.repair.space import freeze_space
from agentgit.repair.templates import TemplateAnchor, model_templates
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService, public
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata

pytestmark = pytest.mark.integration
TABLES = build_metadata().tables


def enqueue(research, repo, config, task_input, model, observation):
    task = research.create_task(repo, config, task_input.goal)
    branch = research.history.branch(repo)
    queue = JobService(research)
    created = queue.enqueue(
        repo,
        task,
        actor="fixture",
        key=str(task),
        config_version=1,
        expected_branch_version=branch["version"],
        expected_head=branch["head_commit_id"],
    )
    runner = ObservationWorker(queue, observation=observation, model_factory=lambda: model)
    result = runner.run_once()
    assert result["run_id"] == created.run_id
    assert runner.run_once() is None
    with research.history.engine.connect() as connection:
        row = research.history._run(connection, repo, UUID(created.run_id))
    return UUID(created.run_id), row["checkpoint_commit_id"], result


def archive(history, repo, identities, graphs, record, name):
    raw = pack_repository_evidence(
        history,
        dict(
            repository_id=str(repo),
            failed_run_id=str(identities[0]),
            reference_run_id=str(identities[-1]),
            case_id=name,
            graph=graphs[0],
            reference_graph=graphs[-1],
        ),
    )
    with history.engine.connect() as connection:
        table = TABLES["jobs"]
        rows = connection.execute(sa.select(table).where(table.c.repository_id == repo)).mappings()
        raw["indexes"]["jobs"] = [public(dict(row)) for row in rows]
    record.update(graphs=graphs, raw_repository=raw)
    directory = os.environ.get("AGENTGIT_S10_ORIGIN_PROOF_DIR")
    if directory:
        folder = Path(directory).resolve()
        assert folder.parent == (Path.cwd() / ".agentgit/evidence").resolve()
        assert folder.name.startswith("S10-origin-proofs-")
        folder.mkdir(exist_ok=True)
        with (folder / (name + ".json")).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)


@pytest.mark.parametrize("index", range(4))
def test_fixed_regression_input_runs_as_independent_job_with_legal_root_and_no_fake_local(
    index, history, tmp_path
):
    task_input = regression_inputs()[index]
    research = ResearchService(
        history,
        tmp_path / "research",
        corpus=Corpus(task_input.sources, evaluation_time=task_input.evaluation_time),
    )
    repo = history.create_repository("固定回归输入与公开起点")
    config = research.create_config(repo)
    validator = ValidatorService(history).freeze(repo, task_input.goal, research.corpus)
    run, head, result = enqueue(
        research, repo, config, task_input, MockResearchModel(), "canonical"
    )
    assert result["status"] == "completed"
    assessment = ValidatorService(history).evaluate(repo, validator, run, head)
    assert assessment.verdict == "passed"
    origins = select_origins(history, repo, run, validator)
    assert origins["root"]["phase"] == "act"
    assert origins["root"]["inherited_usage"]["model_calls"] == 1
    assert origins["first_exception"] is None and origins["local"] is None
    graph = BehaviorService(history).trace(repo, run).model_dump(mode="json")
    archive(
        history,
        repo,
        [run],
        [graph],
        dict(
            scope="independent-regression-normal-job/no-repair-result",
            public_input=task_input.model_dump(mode="json"),
            assessments=[assessment.model_dump(mode="json")],
            origins=origins,
            jobs=[result],
            archive_role="successful-control-run/failed-slot-alias-only",
        ),
        f"regression-{index}",
    )


def test_empty_observation_fault_and_healthy_catalog_have_real_jobs_and_local_candidates(
    history, tmp_path
):
    # 使用独立回归输入验证通用机制，不实例化 R21–R30 的保留任务。
    task_input = regression_inputs()[0]
    condition = FaultCondition(rank="empty_fallback")
    research = ResearchService(
        history,
        tmp_path / "research",
        corpus=Corpus(task_input.sources, evaluation_time=task_input.evaluation_time),
    )
    repo = history.create_repository("空搜索回退机制与合法局部空间")
    config = research.create_config(repo)
    validators = ValidatorService(history)
    validator = validators.freeze(repo, task_input.goal, research.corpus)
    records = [
        enqueue(research, repo, config, task_input, FaultResearchModel(condition), observation)
        for observation in ("empty", "canonical")
    ]
    assert [r[2]["status"] for r in records] == ["failed", "completed"]
    assessments = [validators.evaluate(repo, validator, run, head) for run, head, _ in records]
    assert [a.verdict for a in assessments] == ["failed", "passed"]
    origins = select_origins(history, repo, records[0][0], validator)
    assert origins["root"]["event"] == origins["local"]["event"]
    assert origins["local"]["event"]["seq"] < origins["first_exception"]["seq"]
    assert origins["local"]["inherited_usage"]["tool_calls"] == 0
    context = origins["local"]["context"]
    view = VersionService(history).checkout(repo, context["base_commit"])
    anchor = TemplateAnchor.model_validate(
        {
            k: context[k]
            for k in (
                "source_run_id",
                "base_commit",
                "target_commit_id",
                "target_event_id",
                "precondition",
            )
        }
    )
    generated = model_templates(
        anchor,
        parse_contract(encode(context["config"]), AgentConfig),
        parse_contract(encode(view["snapshot"]), StateSnapshot),
        history.objects.get,
    )
    assert len(generated.patches) == 2
    assert all(
        p.intervention.target_event_id == context["target_event_id"] for p in generated.patches
    )
    graphs = [BehaviorService(history).trace(repo, r[0]).model_dump(mode="json") for r in records]
    archive(
        history,
        repo,
        [r[0] for r in records],
        graphs,
        dict(
            scope="generic-empty-observation-mechanism/no-heldout-task-no-repair-result",
            public_input=task_input.model_dump(mode="json"),
            condition=condition.model_dump(mode="json"),
            assessments=[a.model_dump(mode="json") for a in assessments],
            origins=origins,
            jobs=[r[2] for r in records],
            local_patches=[p.model_dump(mode="json") for p in generated.patches],
        ),
        "empty-observation",
    )


def test_late_failure_selects_actual_local_model_space_instead_of_filtering_root_to_empty(
    history, tmp_path
):
    task_input = regression_inputs()[0]
    condition = FaultCondition(selection="wrong_value")
    research = ResearchService(
        history,
        tmp_path / "research",
        corpus=Corpus(task_input.sources, evaluation_time=task_input.evaluation_time),
    )
    repo = history.create_repository("公开晚期失败的真实局部候选")
    config = research.create_config(repo)
    validators = ValidatorService(history)
    validator = validators.freeze(repo, task_input.goal, research.corpus)
    records = [
        enqueue(research, repo, config, task_input, model, "canonical")
        for model in (FaultResearchModel(condition), MockResearchModel())
    ]
    assert [r[2]["status"] for r in records] == ["failed", "completed"]
    assessments = [validators.evaluate(repo, validator, run, head) for run, head, _ in records]
    origins = select_origins(history, repo, records[0][0], validator)
    assert (
        origins["root"]["event"]["seq"]
        < origins["local"]["event"]["seq"]
        < origins["first_exception"]["seq"]
    )
    context = origins["local"]["context"]
    view = VersionService(history).checkout(repo, context["base_commit"])
    snapshot = parse_contract(encode(view["snapshot"]), StateSnapshot)
    assert snapshot.data["completed"][-1] == "write-report"
    anchor = TemplateAnchor.model_validate(
        {
            k: context[k]
            for k in (
                "source_run_id",
                "base_commit",
                "target_commit_id",
                "target_event_id",
                "precondition",
            )
        }
    )
    agent_config = parse_contract(encode(context["config"]), AgentConfig)
    generated = model_templates(anchor, agent_config, snapshot, history.objects.get)
    budget = mock_budget(agent_config)
    space = freeze_space(
        RepairBinding(
            repository_id=str(repo),
            failed_run_id=str(records[0][0]),
            base_commit=context["base_commit"],
            environment_ref=view["manifest"]["environment_ref"],
            validator_ref=validator,
            policy_version=mock_policy_version(budget),
        ),
        generated.patches,
        source_reason="公开轨迹的合法局部起点；不读取故障标签",
    )
    identities = tuple(
        ManagementService(research).create_intervention(repo, p.intervention, f"local-{i}")[
            "intervention_id"
        ]
        for i, p in enumerate(space.patches)
    )
    planned = method_plan(
        "first_exception_local",
        space,
        budget,
        identities,
        first_exception_target=context["target_event_id"],
    )
    assert len(planned.space.patches) == len(planned.intervention_ids) == 2
    assert origins["local"]["inherited_usage"]["tool_calls"] == 8
    graphs = [BehaviorService(history).trace(repo, r[0]).model_dump(mode="json") for r in records]
    archive(
        history,
        repo,
        [r[0] for r in records],
        graphs,
        dict(
            scope="generic-late-failure-local-space/no-heldout-task-no-search-result",
            public_input=task_input.model_dump(mode="json"),
            condition=condition.model_dump(mode="json"),
            assessments=[a.model_dump(mode="json") for a in assessments],
            origins=origins,
            jobs=[r[2] for r in records],
            local_space=planned.space.model_dump(mode="json"),
            local_intervention_ids=list(planned.intervention_ids),
        ),
        "late-local-space",
    )


def test_failure_before_any_act_falls_back_to_first_legal_plan_checkpoint(history, tmp_path):
    class EmptyPlanModel:
        signature = object_digest(
            ObjectType.CALL,
            encode(
                {
                    "schema_version": 1,
                    "model": "empty-plan-control/v1",
                }
            ),
        )

        def invoke(self, payload):
            return b""

    task_input = regression_inputs()[0]
    research = ResearchService(
        history,
        tmp_path / "research",
        corpus=Corpus(task_input.sources, evaluation_time=task_input.evaluation_time),
    )
    repo = history.create_repository("首个执行决策之前失败的合法起点")
    config = research.create_config(repo)
    validators = ValidatorService(history)
    validator = validators.freeze(repo, task_input.goal, research.corpus)
    run, head, result = enqueue(research, repo, config, task_input, EmptyPlanModel(), "canonical")
    assert result["status"] == "failed" and result["error_code"] == "model_empty"
    assessment = validators.evaluate(repo, validator, run, head)
    origins = select_origins(history, repo, run, validator)
    assert (
        origins["root"]["phase"] == "plan"
        and origins["root"]["inherited_usage"]["model_calls"] == 0
    )
    assert origins["root"]["event"] == origins["local"]["event"]
    graph = BehaviorService(history).trace(repo, run).model_dump(mode="json")
    archive(
        history,
        repo,
        [run],
        [graph],
        dict(
            scope="generic-initial-plan-failure/no-heldout-task-no-search-result",
            public_input=task_input.model_dump(mode="json"),
            assessments=[assessment.model_dump(mode="json")],
            origins=origins,
            jobs=[result],
            model_signature=EmptyPlanModel.signature,
            archive_role="failed-source-run/reference-slot-alias-only",
        ),
        "initial-plan",
    )
