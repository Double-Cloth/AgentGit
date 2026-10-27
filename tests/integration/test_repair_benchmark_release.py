"""冻结包含生成条件、完整输入、预算及源代码；门禁不能由文件名或预填成功绕过。"""

from copy import deepcopy

import pytest
from repair_benchmark_protocol import METHODS
from repair_benchmark_release import (
    FROZEN,
    Declarations,
    build_release,
    load_declarations,
    load_release,
    training_gate,
)
from test_repair_benchmark_archive import fixture_archive

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract


def test_release_binds_all_cases_methods_regressions_and_behavior_files():
    release = build_release()
    assert release.method_order == METHODS and len(release.cases) == 30
    assert len({case.public_input_ref for case in release.cases}) == 30
    assert len(set(release.regression_input_refs)) == 4
    assert set(release.frozen_files) == set(FROZEN)
    assert release.max_parallel_cases == 4 and release.model_interface_signature
    assert len({case.fixture_ref for case in release.cases}) == 30


@pytest.mark.parametrize("damage", ["none", "source", "method_order", "case_condition", "file_set"])
def test_release_rejects_changed_conditions_without_overwriting_original(tmp_path, damage):
    release = build_release()
    value = release.model_dump(mode="json")
    if damage == "source":
        value["source_revision"] = "changed"
    elif damage == "method_order":
        value["method_order"].reverse()
    elif damage == "case_condition":
        value["cases"][0]["condition_ref"] = "0" * 64
    elif damage == "file_set":
        value["frozen_files"].pop(next(iter(value["frozen_files"])))
    path = tmp_path / "release.json"
    path.write_bytes(encode(value))
    before = path.read_bytes()
    if damage == "none":
        assert load_release(path) == release
    else:
        with pytest.raises(ValueError):
            load_release(path)
    assert path.read_bytes() == before


def test_heldout_gate_rejects_missing_training_evidence(tmp_path):
    with pytest.raises(FileNotFoundError):
        training_gate(build_release(), tmp_path)


def test_heldout_gate_rejects_five_prefilled_statuses_without_actual_jobs(tmp_path):
    release = build_release()
    folder = tmp_path / "cases/R01"
    folder.mkdir(parents=True)
    manifest, _ = fixture_archive(folder)
    manifest["record"] = dict(
        runtime_release_ref=release.identity(),
        case_id="R01",
        scope="frozen-evaluation/v1",
        methods={method: dict(status="verified_under_fixture") for method in METHODS},
    )
    (folder / "case.json").write_bytes(encode(manifest))
    with pytest.raises(ValueError, match="Job"):
        training_gate(release, tmp_path)


@pytest.mark.parametrize(
    "damage", ["missing", "answer_for_no_solution", "adoptable_observation", "score"]
)
def test_declarations_are_complete_metadata_and_cannot_prefill_results(damage):
    original = load_declarations()
    value = deepcopy(original.model_dump(mode="json"))
    if damage == "missing":
        value["cases"].pop()
    elif damage == "answer_for_no_solution":
        value["cases"][15]["solution_templates"] = ["prompt-verified-sources/v1"]
    elif damage == "adoptable_observation":
        value["cases"][12]["solution_templates"] = ["plan-required-artifacts/v1"]
    else:
        value["cases"][0]["passed"] = True
    with pytest.raises(AgentGitError):
        parse_contract(encode(value), Declarations)
