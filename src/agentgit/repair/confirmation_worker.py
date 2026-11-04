"""推进已开启池的确认/回归；普通 Worker 负责实际模型与工具。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.services.repair_confirm_trials import RepairConfirmationTrialService
from agentgit.services.repair_confirmation import RepairConfirmationService
from agentgit.services.research import ResearchService
from agentgit.storage.schema_s10_confirmation import STUDY_TERMINAL


class RepairConfirmationWorker:
    def __init__(
        self,
        research: ResearchService,
        *,
        name: str = "repair-confirm-worker",
        lease_ms: int = 120_000,
        fault: Callable[[str], None] | None = None,
    ):
        self.control = RepairConfirmationService(research, lease_ms=lease_ms)
        self.trials = RepairConfirmationTrialService(research, fault=fault)
        self.name = name

    def run_once(self, *, repository: UUID | None = None) -> dict[str, Any] | None:
        lease = self.control.claim(self.name, repository=repository)
        if lease is None:
            return None
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        try:
            view = self.control.view(repo, study)
            if view["cancel_requested"]:
                self.trials.cancel_children(lease)
                view = self.control.view(repo, study)
            for trial in view["trials"]:
                if "result" not in trial and "dispatch" in trial:
                    self.trials.settle(lease, UUID(trial["trial_id"]))
            view = self.control.view(repo, study)
            if view["status"] in STUDY_TERMINAL:
                return view
            if view["usage"]["active"]:
                if not view["cancel_requested"] and view["status"] != "waiting_reconciliation":
                    for trial in view["trials"]:
                        if "result" not in trial and "dispatch" not in trial:
                            self.trials.dispatch(lease, UUID(trial["trial_id"]))
                return self.control.view(repo, study)
            if view["cancel_requested"]:
                return self.control.finish(lease, reason="operator_cancelled")
            receipts = [trial["result"] for trial in view["trials"] if "result" in trial]
            if any(receipt["safety_violations"] for receipt in receipts):
                return self.control.finish(lease, reason="safety_violation")
            if any(
                "observed_usage_exceeds_reservation" in receipt["uncertainties"]
                for receipt in receipts
            ):
                return self.control.finish(lease, reason="usage_above_reservation")
            if len(view["trials"]) == 10:
                return self.control.finish(lease)
            reserved = self.control.reserve(lease)
            if reserved["admitted"]:
                self.trials.dispatch(lease, UUID(reserved["trial_id"]))
                return self.control.view(repo, study)
            if reserved.get("finished"):
                return self.control.finish(lease)
            if reserved["decision"]["reason"] == "concurrency":
                return self.control.view(repo, study)
            return self.control.finish(lease, reason="budget_" + reserved["decision"]["reason"])
        except AgentGitError as error:
            if error.code in (ErrorCode.VERSION_CONFLICT, ErrorCode.CONTROL_REQUESTED):
                return self.control.view(repo, study)
            with self.control.history.engine.begin() as connection:
                job = self.control.ledger._leased(connection, lease)
                self.control.ledger._update(
                    connection,
                    job,
                    "validation_needs_attention",
                    {"error_code": error.code.value},
                    status="needs_attention",
                    stop_reason=error.code.value,
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            return self.control.view(repo, study)
        finally:
            try:
                self.control.ledger.release(lease)
            except AgentGitError as error:
                if error.code != ErrorCode.VERSION_CONFLICT:
                    raise
