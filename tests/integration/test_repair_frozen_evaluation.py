"""冻结后的三十例五方法执行；结果失败不隐去，保留门禁在执行前检查。"""

import pytest
from repair_benchmark_protocol import METHODS
from repair_benchmark_release import OUTPUT
from repair_dataset_execution import execute_frozen_case

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("case_id", [f"R{i:02}" for i in range(1, 31)])
def test_frozen_case_executes_every_method_and_archives_all_outcomes(case_id, history, tmp_path):
    record, archive = execute_frozen_case(
        history, tmp_path / "research", case_id, OUTPUT / "cases" / case_id
    )
    assert record["scope"] == "frozen-evaluation/v1" and record["runtime_release_ref"]
    assert record["method_order"] == list(METHODS) and set(record["methods"]) == set(METHODS)
    assert record["source_before"] == record["source_after"]
    assert "driver_failure" not in record and "active_method" not in record
    assert archive["objects"] > 0 and archive["raw_bytes"] > 0
    for outcome in record["methods"].values():
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
        assert outcome["suite_id"]
