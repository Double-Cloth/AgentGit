"""沿用 study 租约推进锁前字段阶段，不建立第二套调度租约。"""

from typing import Any
from uuid import UUID

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair_fields import RepairFieldState
from agentgit.core.repair_studies import RepairStudyLease
from agentgit.repair.field_reduction import summarize_fields
from agentgit.services.repair_field_trials import RepairFieldTrialService
from agentgit.services.repair_fields import RepairFieldService
from agentgit.services.research import ResearchService
from agentgit.storage.schema_s10_confirmation import STUDY_TERMINAL


class RepairFieldDriver:
    def __init__(self, research: ResearchService):
        self.fields = RepairFieldService(research.history)
        self.ledger, self.trials = self.fields.ledger, RepairFieldTrialService(research)

    def run_once(self, lease: RepairStudyLease) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        try:
            return self._run(lease, repo, study)
        except AgentGitError as error:
            if error.code in (ErrorCode.VERSION_CONFLICT, ErrorCode.CONTROL_REQUESTED):
                raise
            with self.ledger.history.engine.begin() as connection:
                job = self.ledger._leased(connection, lease)
                self.ledger._update(
                    connection,
                    job,
                    "field_needs_attention",
                    dict(error_code=error.code.value),
                    status="needs_attention",
                    stop_reason=error.code.value,
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            return self.ledger.view(repo, study)

    def _run(self, lease: RepairStudyLease, repo: UUID, study: UUID) -> dict[str, Any]:
        view = self.ledger.view(repo, study)
        if view["cancel_requested"]:
            self.trials.cancel_children(lease)
            view = self.ledger.view(repo, study)
            if view["status"] in STUDY_TERMINAL:
                return view
        field_view = view["field_reduction"]
        for trial in field_view["trials"]:
            if "result" not in trial and "dispatch" in trial:
                self.trials.settle(lease, UUID(trial["trial_id"]))
        view = self.ledger.view(repo, study)
        if view["status"] in STUDY_TERMINAL:
            return view
        if view["usage"]["active"]:
            if view["status"] == "reducing" and not view["cancel_requested"]:
                for trial in view["field_reduction"]["trials"]:
                    if "result" not in trial and "dispatch" not in trial:
                        self.trials.dispatch(lease, UUID(trial["trial_id"]))
            return self.ledger.view(repo, study)
        if view["cancel_requested"]:
            self.trials.cancel_children(lease)
            return self.ledger.view(repo, study)
        state = parse_contract(encode(view["field_reduction"]["state"]), RepairFieldState)
        summary = summarize_fields(state)
        if summary.status in ("needs_review", "cancelled"):
            with self.ledger.history.engine.begin() as connection:
                job = self.ledger._leased(connection, lease)
                self.ledger._update(
                    connection,
                    job,
                    "field_incomplete",
                    {},
                    status="needs_review",
                    stop_reason=summary.reason,
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            return self.ledger.view(repo, study)
        if summary.status in ("complete", "budget_exhausted"):
            return self.fields.seal(lease)
        reserved = self.fields.reserve(lease)
        if reserved["admitted"]:
            self.trials.dispatch(lease, UUID(reserved["trial_id"]))
            return self.ledger.view(repo, study)
        if reserved.get("finished"):
            return self.fields.seal(lease)
        if reserved["decision"]["reason"] == "concurrency":
            return self.ledger.view(repo, study)
        return self.fields.seal(lease, budget_stopped=True)
