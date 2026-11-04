"""新数据开发驱动；所有方法复用生产控制器及 Worker，禁止原型访问保留任务。"""

import time
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from repair_benchmark_archive import write_case
from repair_benchmark_environment import ObservationWorker, source_observation
from repair_benchmark_origins import select_origins
from repair_benchmark_protocol import METHODS, common_budget_ref, method_plan
from repair_benchmark_regressions import regression_inputs
from repair_case_execution import drive
from repair_dataset import input_ref, load_inputs, task_for_case
from repair_fault_runtime import FaultResearchModel, runtime_input
from repair_fault_specifications import load_faults

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.repair import NewRepairSearch, RepairTemplateTarget
from agentgit.core.repair_pools import RegressionOrigin
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.repair_templates import prepare_templates
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


def run_source(research, repository, config, public, condition, observation):
    task = research.create_task(repository, config, public.goal)
    branch = research.history.branch(repository)
    queue = JobService(research)
    made = queue.enqueue(
        repository,
        task,
        actor="benchmark",
        key=str(task),
        config_version=1,
        expected_branch_version=branch["version"],
        expected_head=branch["head_commit_id"],
    )
    runner = ObservationWorker(
        queue, observation=observation, model_factory=lambda: FaultResearchModel(condition)
    )
    outcome = runner.run_once()
    assert outcome["run_id"] == made.run_id and runner.run_once() is None
    with research.history.engine.connect() as connection:
        row = research.history._run(connection, repository, UUID(made.run_id))
    return dict(
        run_id=made.run_id,
        job_id=made.job_id,
        head=row["checkpoint_commit_id"],
        status=outcome["status"],
        error_code=outcome["error_code"],
        observation=observation,
    )


def prepare_case(history, data_root, case, profiles, condition):
    public = runtime_input(task_for_case(case, profiles), condition)
    research = ResearchService(
        history, data_root, corpus=Corpus(public.sources, evaluation_time=public.evaluation_time)
    )
    repository = history.create_repository("冻结开发输入的五方法执行")
    config = research.create_config(repository)
    validators = ValidatorService(history)
    validator = validators.freeze(repository, public.goal, research.corpus)
    started = time.monotonic()
    failed = run_source(
        research, repository, config, public, condition, source_observation(condition)
    )
    assert failed["status"] == "failed"
    assessment = validators.evaluate(repository, validator, UUID(failed["run_id"]), failed["head"])
    assert assessment.verdict == "failed"
    origins = select_origins(history, repository, UUID(failed["run_id"]), validator)
    regressions, regression_records = [], []
    for public_regression in regression_inputs():
        service = ResearchService(
            history,
            data_root,
            corpus=Corpus(
                public_regression.sources, evaluation_time=public_regression.evaluation_time
            ),
        )
        regression_validator = validators.freeze(repository, public_regression.goal, service.corpus)
        source = run_source(
            service, repository, config, public_regression, condition, source_observation(condition)
        )
        assert source["status"] in ("failed", "completed")
        selected = select_origins(history, repository, UUID(source["run_id"]), regression_validator)
        regressions.append(
            RegressionOrigin(
                source_run_id=source["run_id"],
                from_commit=selected["root"]["context"]["base_commit"],
            )
        )
        regression_records.append(
            dict(
                input_ref=input_ref(public_regression),
                source=source,
                root=selected["root"],
                validator_ref=regression_validator,
            )
        )
    graph = BehaviorService(history).trace(repository, UUID(failed["run_id"]))
    target = next(
        node
        for node in sorted(graph.nodes, key=lambda n: n.event.seq)
        if node.event_type == "tool.started"
        and node.payload_verified
        and node.occurrence
        and node.occurrence.provider == "tool:corpus_search/v1"
    )
    with history.engine.connect() as connection:
        source_call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.repository_id == repository,
                    TABLES["external_calls"].c.run_id == UUID(failed["run_id"]),
                    TABLES["external_calls"].c.logical_step_id == target.occurrence.logical_step_id,
                    TABLES["external_calls"].c.call_index == target.occurrence.call_index,
                    TABLES["external_calls"].c.attempt == target.occurrence.attempt,
                )
            )
            .mappings()
            .one()
        )
    original_response = history.objects.get_json(source_call["result_ref"], ObjectType.CALL)[
        "response_ref"
    ]
    observation = dict(
        target_event_id=target.event.event_id,
        source_call_id=str(source_call["id"]),
        references=[],
        replacement_call_ids=[],
        unchanged_call_ids=[],
    )
    # 每例都准备相同的两类只读录制；不看类别、参考运行成败或修复答案选录制。
    for reference_profile in ("canonical", "group_rotated"):
        reference = run_source(research, repository, config, public, condition, reference_profile)
        assert reference["status"] in ("failed", "completed")
        with history.engine.connect() as connection:
            calls = list(
                connection.execute(
                    sa.select(TABLES["external_calls"]).where(
                        TABLES["external_calls"].c.repository_id == repository,
                        TABLES["external_calls"].c.run_id == UUID(reference["run_id"]),
                        TABLES["external_calls"].c.provider == "tool:corpus_search/v1",
                    )
                ).mappings()
            )
        assert len(calls) == 1 and calls[0]["status"] == "succeeded"
        response = history.objects.get_json(calls[0]["result_ref"], ObjectType.CALL)["response_ref"]
        unchanged = response == original_response
        observation["references"].append(
            dict(
                source=reference,
                call_id=str(calls[0]["id"]),
                response_ref=response,
                unchanged=unchanged,
            )
        )
        observation["unchanged_call_ids" if unchanged else "replacement_call_ids"].append(
            str(calls[0]["id"])
        )
    before = (
        BehaviorService(history).trace(repository, UUID(failed["run_id"])).model_dump(mode="json")
    )
    record = dict(
        scope="development-driver-prototype/not-frozen-evaluation",
        case_id=case.id,
        public_input_ref=input_ref(public),
        public_input=public.model_dump(mode="json"),
        condition=condition.model_dump(mode="json"),
        condition_ref=condition.identity(),
        model_signature=FaultResearchModel(condition).signature,
        fixture_model_ref=FaultResearchModel(condition).fixture_ref,
        source=failed,
        source_assessment=assessment.model_dump(mode="json"),
        origins=origins,
        regression_preparation=regression_records,
        observation=observation,
        preparation_wall_ms=round((time.monotonic() - started) * 1000),
        source_before=before,
        methods={},
    )
    return research, repository, validator, condition, tuple(regressions), record


def execute_method(research, repository, validator, condition, regressions, record, method):
    started = time.monotonic()
    selected = record["origins"]["local" if method == "first_exception_local" else "root"]
    assert selected is not None, "该开发原型尚不接受完全没有合法局部模型点的任务"
    context = selected["context"]
    targets = [RepairTemplateTarget(event_id=context["target_event_id"])]
    hybrid = None
    if record["observation"] is not None and method != "first_exception_local":
        observation = record["observation"]
        targets.append(
            RepairTemplateTarget(
                event_id=observation["target_event_id"],
                replacement_call_ids=tuple(observation["replacement_call_ids"]),
            )
        )
        hybrid = HybridPolicy(
            until_commit=record["source"]["head"], calls=(observation["source_call_id"],)
        )
    templates = prepare_templates(
        research.history,
        repository,
        NewRepairSearch(
            failed_run_id=record["source"]["run_id"],
            base_commit=context["base_commit"],
            validator_ref=validator,
            targets=tuple(targets),
            hybrid=hybrid,
        ),
    )
    manager = ManagementService(research)
    identities = tuple(
        manager.create_intervention(repository, patch.intervention, f"{method}-atom-{i}")[
            "intervention_id"
        ]
        for i, patch in enumerate(templates.space.patches)
    )
    planned = method_plan(
        method,
        templates.space,
        templates.budget,
        identities,
        first_exception_target=record["origins"]["local"]["context"]["target_event_id"]
        if record["origins"]["local"]
        else None,
    )
    made = RepairSearchService(research.history).submit(
        planned.space,
        planned.budget,
        planned.intervention_ids,
        actor="benchmark",
        key=method,
        hybrid=hybrid,
        _regression=regressions,
        _research=research,
    )
    search = UUID(made["search_id"])
    suite = manager.repair_workflow(repository, search)["suite_id"]
    assert suite
    runner = ObservationWorker(
        JobService(research),
        observation=source_observation(condition),
        model_factory=lambda: FaultResearchModel(condition),
    )
    screened = drive(research, repository, search, runner, "screen")
    outcome = dict(
        method=method,
        method_ref=planned.identity(),
        common_budget_ref=common_budget_ref(planned.budget),
        source_origin=selected,
        space=planned.space.model_dump(mode="json"),
        budget=planned.budget.model_dump(mode="json"),
        search_id=str(search),
        suite_id=suite,
        source=screened,
        status=screened["status"],
    )
    if screened["status"] == "needs_confirmation":
        made = RepairStudyService(research.history).submit(
            repository,
            NewRepairStudy(
                search_id=str(search),
                max_reduce_trials=planned.max_reduce_trials,
                field_policy=planned.field_policy,
            ),
            actor="benchmark",
            key="study:" + method,
        )
        study = UUID(made["study_id"])
        reduced = drive(research, repository, study, runner, "reduce")
        outcome.update(study_id=str(study), reduced=reduced, status=reduced["status"])
        if reduced["status"] == "awaiting_samples":
            opened = RepairValidationPoolService(research).open(
                repository, study, UUID(suite), actor="benchmark", key="open:" + method
            )
            outcome["opening"] = opened
            if opened["opened"]:
                final = drive(research, repository, study, runner, "confirm")
                outcome.update(validation=final, status=final["status"])
            else:
                outcome["stopping_reason"] = "validation_preflight_blocked"
    outcome["execution_wall_ms"] = round((time.monotonic() - started) * 1000)
    return outcome


def _execute_case(history, data_root, case_id, folder, methods, release=None):
    specifications, profiles, _ = load_inputs()
    case = next(case for case in specifications.cases if case.id == case_id)
    runtime_case = next(case for case in load_faults().cases if case.id == case_id)
    if release is None:
        assert case.split == "train", "原型禁止访问保留任务，必须先冻结完整运行 release"
    else:
        assert methods == METHODS
    assert methods and len(set(methods)) == len(methods) and all(m in METHODS for m in methods)
    research, repository, validator, condition, regressions, record = prepare_case(
        history, data_root, case, profiles, runtime_case.condition
    )
    record["runtime_case"] = runtime_case.model_dump(mode="json", exclude={"condition"})
    record["method_order"] = list(methods)
    prefix = "prototype" if release is None else "evaluation"
    if release is not None:
        record["scope"] = "frozen-evaluation/v1"
        record["runtime_release_ref"] = release.identity()
    print(f"{prefix}:{case_id}:prepared", flush=True)
    try:
        for method in methods:
            if release is not None:
                from repair_benchmark_release import load_release

                assert load_release() == release
            print(f"{prefix}:{case_id}:{method}:started", flush=True)
            record["active_method"] = method
            record["methods"][method] = execute_method(
                research, repository, validator, condition, regressions, record, method
            )
            print(f"{prefix}:{case_id}:{method}:" + record["methods"][method]["status"], flush=True)
        del record["active_method"]
    except Exception as error:
        record["driver_failure"] = dict(type=type(error).__name__, message=str(error))
        record["source_after"] = (
            BehaviorService(history)
            .trace(repository, UUID(record["source"]["run_id"]))
            .model_dump(mode="json")
        )
        write_case(history, repository, folder, record)
        raise
    record["source_after"] = (
        BehaviorService(history)
        .trace(repository, UUID(record["source"]["run_id"]))
        .model_dump(mode="json")
    )
    assert record["source_before"] == record["source_after"]
    assert len({r["common_budget_ref"] for r in record["methods"].values()}) == 1
    archive = write_case(history, repository, folder, record)
    return record, archive


def execute_development_case(history, data_root, case_id, folder: Path, *, methods=METHODS):
    return _execute_case(history, data_root, case_id, folder, methods)


def execute_frozen_case(history, data_root, case_id, folder: Path):
    from repair_benchmark_release import case_permission

    release, binding = case_permission(case_id, folder)
    record, archive = _execute_case(history, data_root, case_id, folder, METHODS, release)
    assert record["public_input_ref"] == binding.public_input_ref
    assert record["condition_ref"] == binding.condition_ref
    assert record["fixture_model_ref"] == binding.fixture_ref
    return record, archive
