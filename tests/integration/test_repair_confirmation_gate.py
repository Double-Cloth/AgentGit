"""五次通过加一次执行错误不能被数据库或统计层当作完整 5/6。"""

from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError
from test_repair_confirmation import actual_worker, setup_confirmation

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import parse_contract
from agentgit.core.repair_validation import RepairVerificationSummary
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.services.jobs import JobService
from agentgit.services.repair_confirmation import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


class InvalidResponse(MockResearchModel):
    """受控传输/Schema 故障；完整保存坏响应，不把执行错误作为硬失败。"""

    def invoke(self, payload):
        return b"invalid-json"


def test_five_passes_plus_incomplete_confirmation_cannot_be_promoted_to_verified(history, tmp_path):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    runner = RepairConfirmationWorker(research)
    runner.run_once(repository=repo)
    assert (
        Worker(JobService(research), model_factory=InvalidResponse).run_once()["status"] == "failed"
    )
    worker = actual_worker(history, research, repo, "pass")
    for _ in range(9):
        assert runner.run_once(repository=repo)["status"] in ("confirming", "regressing")
        assert worker.run_once()["status"] == "completed"
    control = runner.control
    lease = control.claim("last-settlement", repository=repo)
    view = control.view(repo, study)
    runner.trials.settle(lease, UUID(view["trials"][-1]["trial_id"]))
    before = control.view(repo, study)
    assert before["provisional_verification"]["status"] == "needs_review"
    assert before["provisional_verification"]["confirm_passed"] == 5
    assert before["provisional_verification"]["confirm_determinate"] == 5
    assert before["provisional_verification"]["regression_passed"] == 4
    summary = parse_contract(encode(before["provisional_verification"]), RepairVerificationSummary)
    with history.engine.begin() as connection, pytest.raises(DBAPIError):
        job = control.ledger._leased(connection, lease)
        body = history.objects.put_json(ObjectType.BLOB, summary.model_dump(mode="json"))
        history._ref(connection, repo, body, "repair_study", str(study), "validation_summary")
        event, _ = control.ledger._event(
            connection,
            repo,
            study,
            job["revision"] + 1,
            "validation_finished",
            dict(summary_ref=body.id, reason="malicious_promotion"),
        )
        connection.execute(
            TABLES["repair_validation_summaries"]
            .insert()
            .values(id=study, repository_id=repo, summary_ref=body.id, event_id=event)
        )
        connection.execute(
            TABLES["repair_study_jobs"]
            .update()
            .where(TABLES["repair_study_jobs"].c.study_id == study)
            .values(
                status="verified_under_fixture",
                revision=job["revision"] + 1,
                lease_token=None,
                lease_worker=None,
                lease_until=None,
            )
        )
    final = control.finish(lease)
    assert final["status"] == "needs_review" and not final["verification"]["production_eligible"]
