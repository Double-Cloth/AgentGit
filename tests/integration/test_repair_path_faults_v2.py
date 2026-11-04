"""仅对改动的两个开发路径机制运行源/对照；不访问保留任务。"""

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from repair_benchmark_archive import CaseArchive, write_case
from repair_dataset import load_inputs, task_for_case
from repair_fault_runtime import FaultResearchModel, runtime_input
from repair_fault_specifications import load_faults
from test_repair_benchmark_origins import enqueue

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType
from agentgit.core.models import parse_contract
from agentgit.core.snapshots import StateSnapshot
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("case_id", ["R19", "R20"])
def test_v2_export_path_fault_is_rejected_without_untrusted_instruction_and_control_passes(
    case_id, history, tmp_path
):
    specs = load_faults()
    case = next(c for c in specs.cases if c.id == case_id)
    base, profiles, _ = load_inputs()
    original = task_for_case(next(c for c in base.cases if c.id == case_id), profiles)
    public = runtime_input(original, case.condition)
    assert case.split == "train" and public == original and case.condition.instruction == "none"
    research = ResearchService(
        history,
        tmp_path / "research",
        corpus=Corpus(public.sources, evaluation_time=public.evaluation_time),
    )
    repo = history.create_repository("独立导出路径缺陷 v2")
    config = research.create_config(repo)
    validators = ValidatorService(history)
    validator = validators.freeze(repo, public.goal, research.corpus)
    runs = [
        enqueue(research, repo, config, public, model, "canonical")
        for model in (FaultResearchModel(case.condition), MockResearchModel())
    ]
    assessments = [validators.evaluate(repo, validator, run, head) for run, head, _ in runs]
    assert [result["status"] for _, _, result in runs] == ["failed", "completed"]
    assert [a.verdict for a in assessments] == ["failed", "passed"]
    assert runs[0][2]["error_code"] == "loop_detected"
    snapshot = parse_contract(
        history.objects.get(assessments[0].state_ref, ObjectType.STATE), StateSnapshot
    )
    assert not snapshot.artifacts
    with history.engine.connect() as connection:
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.repository_id == repo,
                    TABLES["external_calls"].c.run_id == runs[0][0],
                    TABLES["external_calls"].c.provider == "tool:artifact_write/v1",
                )
            ).mappings()
        )
    assert len(calls) == 2
    assert all(
        history.objects.get_json(c["result_ref"], ObjectType.CALL)["error"]["code"] == "validation"
        for c in calls
    )
    parent = Path(os.environ.get("AGENTGIT_S10_PATH_V2_DIR", str(tmp_path / "archive"))).resolve()
    if "AGENTGIT_S10_PATH_V2_DIR" in os.environ:
        assert parent.parent == (Path.cwd() / ".agentgit/evidence").resolve()
        assert parent.name.startswith("S10-path-v2-")
    write_case(
        history,
        repo,
        parent / case_id,
        dict(
            scope="train-path-v2-construction/no-evaluation-score",
            case_id=case_id,
            public_input=public.model_dump(mode="json"),
            runtime_case=case.model_dump(mode="json"),
            fixture_model_ref=FaultResearchModel(case.condition).fixture_ref,
            runs=[dict(run_id=str(run), head=head, result=result) for run, head, result in runs],
            assessments=[a.model_dump(mode="json") for a in assessments],
        ),
    )
    CaseArchive(parent / case_id).verify_objects()
