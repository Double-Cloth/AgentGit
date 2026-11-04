"""仅为二十个开发输入执行正常控制，未运行故障、搜索或保留集。"""

import json
import os
from pathlib import Path

import pytest
from diagnosis_evidence import pack_repository_evidence
from repair_dataset import (
    DATA,
    InputSpecifications,
    Profiles,
    input_ref,
    task_for_case,
    verify_inputs,
)

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
def test_new_training_input_completes_normal_run_with_original_hard_oracle(
    case_id, history, tmp_path
):
    specifications = parse_contract((DATA / "cases.json").read_bytes(), InputSpecifications)
    profiles = parse_contract((DATA / "inputs.json").read_bytes(), Profiles)
    verify_inputs(specifications, profiles)
    case = next(case for case in specifications.cases if case.id == case_id)
    assert case.split == "train"
    task_input = task_for_case(case, profiles)
    corpus = Corpus(task_input.sources, evaluation_time=task_input.evaluation_time)
    research = ResearchService(history, tmp_path / "research", corpus=corpus)
    repo = history.create_repository("新公开输入正常控制")
    config = research.create_config(repo)
    validator = ValidatorService(history).freeze(repo, task_input.goal, corpus)
    task = research.create_task(repo, config, task_input.goal)
    session = research.start(repo, task)
    result = session.execute(MockResearchModel())
    assert result.status.value == "completed" and result.evaluation.passed
    assert result.usage.model_calls == 11 and result.usage.tool_calls == 9
    assert result.usage.usage_kind == "mock_utf8_bytes" and result.usage.tokens <= 200000
    assessment = ValidatorService(history).evaluate(repo, validator, session.run_id, session.head)
    assert assessment.verdict == "passed" and assessment.evaluation.passed
    graph = BehaviorService(history).trace(repo, session.run_id).model_dump(mode="json")
    evidence = pack_repository_evidence(
        history,
        dict(
            repository_id=str(repo),
            failed_run_id=str(session.run_id),
            reference_run_id=str(session.run_id),
            case_id=case.id,
            graph=graph,
            reference_graph=graph,
        ),
    )
    model_calls = [
        row for row in evidence["indexes"]["external_calls"] if row["provider"].startswith("llm:")
    ]
    assert len(model_calls) == 11
    for call in model_calls:
        intent = history.objects.get_json(call["intent_ref"], ObjectType.CALL)
        request = parse_contract(
            history.objects.get(intent["request_ref"], ObjectType.BLOB), ModelRequest
        )
        assert request.state.goal == task_input.goal
        assert (
            case.id not in request.state.goal.text and case.pattern not in request.state.goal.text
        )
    directory = os.environ.get("AGENTGIT_S10_INPUT_CONTROL_DIR")
    if directory:
        folder = Path(directory).resolve()
        assert folder.parent == (Path.cwd() / ".agentgit/evidence").resolve()
        assert folder.name.startswith("S10-input-controls-")
        folder.mkdir(exist_ok=True)
        record = dict(
            scope="train-normal-input-control/no-fault-no-search",
            case_id=case.id,
            input_ref=input_ref(task_input),
            public_input=task_input.model_dump(mode="json"),
            source_run_id=str(session.run_id),
            archive_role="successful-control-run/failed-slot-alias-only",
            assessment=assessment.model_dump(mode="json"),
            usage=result.usage.model_dump(mode="json"),
            graph=graph,
            raw_repository=evidence,
        )
        with (folder / f"{case.id}.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
