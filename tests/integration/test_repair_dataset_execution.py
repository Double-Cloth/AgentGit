"""开发例的整链驱动原型；禁止将本节点计入冻结后的三十例研究。"""

import os
from pathlib import Path

import pytest
from repair_benchmark_archive import CaseArchive
from repair_benchmark_protocol import METHODS
from repair_dataset_execution import execute_development_case

pytestmark = pytest.mark.integration


def test_new_joint_case_uses_production_search_study_and_pre_registered_regressions(
    history, tmp_path
):
    parent = Path(
        os.environ.get("AGENTGIT_S10_DATASET_PROTOTYPE_DIR", str(tmp_path / "archive"))
    ).resolve()
    if "AGENTGIT_S10_DATASET_PROTOTYPE_DIR" in os.environ:
        assert parent.parent == (Path.cwd() / ".agentgit/evidence").resolve()
        assert parent.name.startswith("S10-dataset-prototype-")
    record, receipt = execute_development_case(
        history, tmp_path / "research", "R07", parent / "R07"
    )
    archive = CaseArchive(parent / "R07")
    archive.verify_objects()
    assert archive.manifest["record"] == record and receipt["objects"] > 0
    assert record["source_before"] == record["source_after"]
    assert len({r["source"]["run_id"] for r in record["regression_preparation"]}) == 4
    assert tuple(record["methods"]) == METHODS
    assert len(record["observation"]["references"]) == 2
    assert len(record["observation"]["unchanged_call_ids"]) == 1
    assert len(record["observation"]["replacement_call_ids"]) == 1
    assert (
        record["methods"]["repair"]["space"]["patches"]
        == record["methods"]["unordered_enumeration"]["space"]["patches"]
    )
    for method, outcome in record["methods"].items():
        assert outcome["status"] in (
            "verified_under_fixture",
            "rejected",
            "needs_review",
            "budget_exhausted",
            "no_solution_in_space",
            "not_found",
            "needs_attention",
            "cancelled",
            "awaiting_samples",
        )
        assert outcome["suite_id"] and outcome["source"]["manifest"]["budget"]["max_trials"] == 30
        if method == "first_exception_local":
            assert len(outcome["space"]["patches"]) > 0
            assert outcome["source_origin"] == record["origins"]["local"]
        if method == "combination_without_reduction" and "reduced" in outcome:
            assert outcome["reduced"]["trials"] == []
            assert outcome["reduced"]["reduction"]["minimality"] == "not_proven"
            assert "field_reduction" not in outcome["reduced"]
        if "validation" in outcome:
            final = outcome["validation"]
            if final["status"] == "verified_under_fixture":
                assert len(final["trials"]) == 10
                assert all(t["result"] is not None for t in final["trials"])
                assert len({t["result"]["current_run_id"] for t in final["trials"]}) == 10
                assert final["verification"]["confirm_passed"] >= 5
                assert final["verification"]["regression_passed"] == 4
