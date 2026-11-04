"""二十个开发故障的真实源运行和独立正常对照，不执行候选搜索。"""

import json
import os
from pathlib import Path

import pytest
from diagnosis_evidence import pack_repository_evidence
from repair_dataset import input_ref, load_inputs, task_for_case
from repair_fault_runtime import FaultResearchModel, condition_for, runtime_input

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType
from agentgit.core.models import parse_contract
from agentgit.core.research import ModelRequest
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("case_id", [f"R{i:02}" for i in range(1, 21)])
def test_training_fault_reaches_failed_run_without_weakening_oracle_or_permissions(
    case_id,
    history,
    tmp_path,
):
    specifications, profiles, _ = load_inputs()
    case = next(c for c in specifications.cases if c.id == case_id)
    assert case.split == "train"
    condition = condition_for(case)
    public = runtime_input(task_for_case(case, profiles), condition)
    corpus = Corpus(public.sources, evaluation_time=public.evaluation_time)
    research = ResearchService(history, tmp_path / "research", corpus=corpus)
    repo = history.create_repository("开发故障构造与正常对照")
    config = research.create_config(repo)
    validators = ValidatorService(history)
    frozen = validators.freeze(repo, public.goal, corpus)
    sessions, assessments, results = [], [], []
    for model in (FaultResearchModel(condition), MockResearchModel()):
        task = research.create_task(repo, config, public.goal)
        run = research.start(repo, task)
        result = run.execute(model)
        sessions.append(run)
        results.append(result)
        assessments.append(validators.evaluate(repo, frozen, run.run_id, run.head))
    failed, control = sessions
    assert results[0].status.value == "failed" and assessments[0].verdict == "failed"
    assert results[1].status.value == "completed" and assessments[1].verdict == "passed"
    assert results[0].usage.tokens <= 200000 and results[1].usage.tokens <= 200000
    graphs = [
        BehaviorService(history).trace(repo, run.run_id).model_dump(mode="json") for run in sessions
    ]
    raw = pack_repository_evidence(
        history,
        dict(
            repository_id=str(repo),
            failed_run_id=str(failed.run_id),
            reference_run_id=str(control.run_id),
            case_id=case.id,
            graph=graphs[0],
            reference_graph=graphs[1],
        ),
    )
    calls = raw["indexes"]["external_calls"]
    for call in calls:
        if call["provider"].startswith("llm:"):
            intent = history.objects.get_json(call["intent_ref"], ObjectType.CALL)
            request = parse_contract(
                history.objects.get(intent["request_ref"], ObjectType.BLOB), ModelRequest
            )
            assert request.state.goal == public.goal and request.available_tools == (
                "corpus_search",
                "source_read",
                "artifact_write",
            )
            assert (
                case.id not in request.state.goal.text
                and case.pattern not in request.state.goal.text
            )
    # 非授权执行与越界写没有产物；不依靠在宿主上检查攻击目标来制造或清理文件。
    assert set(failed.workspace.contents()) <= {"report.md", "sources.json"}
    if condition.instruction in ("parent_path", "absolute_path"):
        assert not failed.workspace.contents()
        assert results[0].error_code == "loop_detected"
        blocked = [
            history.objects.get_json(c["result_ref"], ObjectType.CALL)
            for c in calls
            if c["run_id"] == str(failed.run_id)
            and c["provider"].startswith("tool:artifact_write/")
        ]
        assert len(blocked) == 2 and all(c["error"]["code"] == "validation" for c in blocked)
        assert not any(
            c["provider"].startswith("tool:artifact_write/") and c["status"] == "succeeded"
            for c in calls
            if c["run_id"] == str(failed.run_id)
        )
    directory = os.environ.get("AGENTGIT_S10_FAULT_SOURCE_DIR")
    if directory:
        folder = Path(directory).resolve()
        assert folder.parent == (Path.cwd() / ".agentgit/evidence").resolve()
        assert folder.name.startswith("S10-fault-sources-")
        folder.mkdir(exist_ok=True)
        record = dict(
            scope="train-fault-construction-and-normal-control/no-search-no-repair-score",
            case_id=case.id,
            public_input=public.model_dump(mode="json"),
            input_ref=input_ref(public),
            condition=condition.model_dump(mode="json"),
            condition_ref=condition.identity(),
            model_signature=FaultResearchModel(condition).signature,
            failed_run_id=str(failed.run_id),
            reference_run_id=str(control.run_id),
            assessments=[a.model_dump(mode="json") for a in assessments],
            usage=[r.usage.model_dump(mode="json") for r in results],
            graphs=graphs,
            raw_repository=raw,
        )
        with (folder / f"{case.id}.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
