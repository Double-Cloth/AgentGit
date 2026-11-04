"""逐例执行全部故障注入；这里只验证构造与标签寻址，不计算定位命中率。"""

import json
import os
from pathlib import Path
from time import monotonic_ns

import pytest
from diagnosis_dataset import load_dataset, run_case
from diagnosis_evidence import pack_repository_evidence

pytestmark = pytest.mark.integration
DATASET = load_dataset()


@pytest.mark.parametrize("case", DATASET.cases, ids=lambda case: case.id)
def test_fault_recipe_produces_recorded_failure_and_resolvable_proposed_labels(
    history, tmp_path, case
):
    started = monotonic_ns()
    result = run_case(history, tmp_path, case, DATASET)
    result["setup_and_execution_ms"] = (monotonic_ns() - started) // 1_000_000
    destination = os.environ.get("AGENTGIT_DIAGNOSIS_EVIDENCE_DIR")
    if destination:
        path = Path(destination).resolve()
        assert path.name.startswith("S8-dataset-") and path.is_relative_to(
            Path.cwd() / ".agentgit/evidence"
        )
        path.mkdir(parents=True, exist_ok=True)
        bundle = pack_repository_evidence(history, result)
        (path / f"{case.id}-evidence.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (path / f"{case.id}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def test_dataset_has_complete_fixed_partition_and_no_claim_of_human_annotation():
    assert len(DATASET.cases) == 20
    assert [case.split for case in DATASET.cases].count("train") == 12
    assert [case.split for case in DATASET.cases].count("heldout") == 8
    assert all(
        case.annotation.review_status == "pending_human"
        and case.annotation.origin == "agent_proposed"
        for case in DATASET.cases
    )
    assert {case.family for case in DATASET.cases} >= {
        "retrieval",
        "empty_response",
        "schema",
        "timeout",
        "plan_omission",
        "memory_conflict",
        "stale_source",
    }
