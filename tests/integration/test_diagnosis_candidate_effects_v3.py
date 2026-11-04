"""补齐二十例的候选效果/不适用证据，不重算定位指标或要求每项修复成功。"""

import pytest
from diagnosis_dataset import load_dataset
from diagnosis_effect_execution_v3 import execute_case

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("case", load_dataset().cases, ids=lambda case: case.id)
def test_frozen_fault_condition_candidate_ab_preserves_every_outcome(history, tmp_path, case):
    result = execute_case(history, tmp_path, case)
    assert result["annotation_status"] == "pending_human"
    assert result["source_before"] == result["source_after"] and result["static_report_unchanged"]
    assert result["effect_status"] in ("static_only", "assessed", "incomplete")
    if result["effect_status"] == "static_only":
        assert result["effect"] is None
