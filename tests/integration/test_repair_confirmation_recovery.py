"""实际模型结果落盘后 Worker 中断，恢复新 Run 只消费本 Job 已有结果。"""

import time
from uuid import UUID

import pytest
from test_repair_confirmation import actual_worker, setup_confirmation

from agentgit.core.canonical import ObjectType
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.services.jobs import JobService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_actual_worker_recovery_proves_own_call_family_without_repeating_saved_model_result(
    history, tmp_path
):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    controller = RepairConfirmationWorker(research)
    view = controller.run_once(repository=repo)
    trial = view["trials"][0]
    initial = trial["dispatch"]["initial_run_id"]
    good = actual_worker(history, research, repo, "pass")

    def fault(stage):
        if stage == "after_result":
            raise RuntimeError("模型结果已落盘但尚未提交运行检查点")

    crashed = Worker(
        JobService(research, lease_ms=1000), model_factory=good.model_factory, fault=fault
    )
    with pytest.raises(RuntimeError):
        crashed.run_once()
    time.sleep(1.15)
    recovered = good.run_once()
    assert recovered["status"] == "completed" and recovered["run_id"] != initial
    lease = controller.control.claim("settle-recovery", repository=repo)
    receipt = controller.trials.settle(lease, UUID(trial["trial_id"]))["result"]
    assert receipt["verdict"] == "passed" and receipt["current_run_id"] == recovered["run_id"]
    assert receipt["initial_run_id"] == initial and receipt["fresh_model_calls"] == 2
    proof = history.objects.get_json(receipt["provenance_ref"], ObjectType.BLOB)
    assert proof["run_ids"] == [recovered["run_id"], initial]
    assert len([call for call in proof["model_calls"] if call["origin_call_id"] is None]) == 2
    assert any(call["origin_call_id"] is not None for call in proof["model_calls"])
    assert controller.control.view(repo, study)["usage"]["active"] == 0
