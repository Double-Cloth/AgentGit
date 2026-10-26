"""独立缩减控制器；模型和工具仍由普通 Worker 执行。"""

from typing import Any
from uuid import UUID

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair import BudgetDecision
from agentgit.core.repair_studies import RepairStudyLease
from agentgit.core.repair_validation import ReductionState
from agentgit.repair.field_worker import RepairFieldDriver
from agentgit.repair.reduction import next_deletion, summarize_reduction
from agentgit.services.repair_reductions import RepairReductionService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.research import ResearchService
from agentgit.storage.schema_s10_confirmation import STUDY_TERMINAL


class RepairStudyWorker:
    def __init__(self, research: ResearchService, *, name: str = "repair-study-worker"):
        self.ledger, self.trials = (
            RepairStudyService(research.history),
            RepairReductionService(research),
        )
        self.name = name
        self.field_driver = RepairFieldDriver(research)

    def _fields(self, lease: RepairStudyLease, *, budget_stopped: bool = False) -> dict[str, Any]:
        self.field_driver.fields.initialize(lease, budget_stopped=budget_stopped)
        return self.field_driver.run_once(lease)

    def run_once(self, *, repository: UUID | None = None) -> dict[str, Any] | None:
        lease = self.ledger.claim(self.name, repository=repository)
        if lease is None:
            return None
        repo, identity = UUID(lease.repository_id), UUID(lease.study_id)
        try:
            view = self.ledger.view(repo, identity)
            if view.get("field_reduction") is not None:
                return self.field_driver.run_once(lease)
            if view["cancel_requested"]:
                self.trials.cancel_children(lease)
                view = self.ledger.view(repo, identity)
                if view["status"] in STUDY_TERMINAL:
                    return view
            for trial in view["trials"]:
                if "result" not in trial and "dispatch" in trial:
                    self.trials.settle(lease, UUID(trial["trial_id"]))
            view = self.ledger.view(repo, identity)
            if (
                view["status"] in (*STUDY_TERMINAL, "waiting_reconciliation")
                or view["cancel_requested"]
            ):
                return view
            for trial in view["trials"]:
                if "result" not in trial:
                    if "dispatch" not in trial:
                        self.trials.dispatch(lease, UUID(trial["trial_id"]))
                    return self.ledger.view(repo, identity)
            state = parse_contract(encode(view["state"]), ReductionState)
            if next_deletion(state) is None:
                if summarize_reduction(state).status in ("needs_review", "cancelled"):
                    with self.ledger.history.engine.begin() as connection:
                        job = self.ledger._leased(connection, lease)
                        self.ledger._update(
                            connection,
                            job,
                            "reduction_incomplete",
                            {},
                            status="needs_review",
                            stop_reason="indeterminate_deletions",
                            lease_token=None,
                            lease_worker=None,
                            lease_until=None,
                        )
                    return self.ledger.view(repo, identity)
                if view["manifest"].get("field_policy") is not None:
                    return self._fields(lease)
                return self.ledger.seal(lease)
            reserved = self.ledger.reserve(lease)
            if reserved["admitted"]:
                self.trials.dispatch(lease, UUID(reserved["trial_id"]))
                return self.ledger.view(repo, identity)
            if reserved.get("finished"):
                if view["manifest"].get("field_policy") is not None:
                    return self._fields(lease)
                return self.ledger.seal(lease)
            decision = parse_contract(encode(reserved["decision"]), BudgetDecision)
            if decision.reason == "concurrency":
                return self.ledger.view(repo, identity)
            if view["manifest"].get("field_policy") is not None:
                return self._fields(lease, budget_stopped=True)
            return self.ledger.seal(lease, budget_stop=decision)
        except AgentGitError as exc:
            if exc.code not in (ErrorCode.VERSION_CONFLICT, ErrorCode.CONTROL_REQUESTED):
                raise
            return self.ledger.view(repo, identity)
        finally:
            try:
                self.ledger.release(lease)
            except AgentGitError as exc:
                if exc.code != ErrorCode.VERSION_CONFLICT:
                    raise
