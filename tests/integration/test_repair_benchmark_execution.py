"""五种方法实际接入现有 Job/Run/缩减/六加四；不是新的研究任务集。"""

import pytest
from repair_benchmark_execution import execute_smoke
from repair_benchmark_protocol import METHODS

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("method", METHODS)
def test_benchmark_methods_use_real_trials_and_keep_inapplicable_local_baseline(
    method, history, tmp_path
):
    result = execute_smoke(history, tmp_path, method)
    assert result["scope"] == "legacy-engineering-fixture/method-wiring-only"
    if not result["selected_space"]["patches"]:
        assert method == "first_exception_local"
        assert result["status"] == "not_found"
        assert "validation" not in result
        assert all(
            t["intent"]["operation"] == "recorded_preflight" for t in result["source"]["trials"]
        )
        return
    assert result["status"] == "verified_under_fixture"
    final = result["validation"]
    assert final["verification"]["confirm_passed"] == 6
    assert final["verification"]["regression_passed"] == 4
    assert len({trial["result"]["current_run_id"] for trial in final["trials"]}) == 10
    assert all(trial["result"]["fresh_model_calls"] > 0 for trial in final["trials"])
    if method == "combination_without_reduction":
        assert result["reduced"]["trials"] == []
        assert result["reduced"]["reduction"]["minimality"] == "not_proven"
        assert "field_reduction" not in result["reduced"]
