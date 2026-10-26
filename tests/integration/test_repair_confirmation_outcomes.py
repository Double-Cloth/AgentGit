"""预先冻结失败向量的 5/6 门槛与固定回归，不冒充随机样本评估。"""

from uuid import UUID

import pytest
from test_repair_confirmation import drive_confirmation, setup_confirmation

from agentgit.services.repair_confirmation import RepairConfirmationService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_studies import RepairStudyService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "policy,confirm,regression,status",
    [
        ("one_confirm_failure", 5, 4, "verified_under_fixture"),
        ("two_confirm_failures", 4, 4, "rejected"),
        ("regression_failure", 6, 3, "rejected"),
    ],
)
def test_frozen_failure_vectors_cannot_reuse_screen_result_or_ignore_regression(
    history, tmp_path, policy, confirm, regression, status
):
    repo, study, research, source, _ = setup_confirmation(history, tmp_path, policy=policy)
    result = drive_confirmation(history, research, repo, policy=policy)
    assert result["status"] == status, result
    summary = result["verification"]
    assert summary["confirm_attempted"] == summary["confirm_determinate"] == 6
    assert summary["regression_attempted"] == 4
    assert summary["confirm_passed"] == confirm and summary["regression_passed"] == regression
    assert len(summary["result_refs"]) == 10 and summary["confirmation_interval"]["samples"] == 6
    assert summary["production_eligible"] == (status == "verified_under_fixture")
    assert (
        RepairJobService(history).view(repo, UUID(source["search_id"]))["decision"]
        == source["decision"]
    )
    assert RepairConfirmationService(research).claim("cannot-reopen", repository=repo) is None
    assert RepairStudyService(history).cancel(repo, study, actor="operator")["status"] == status
