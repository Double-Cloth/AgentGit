"""将缩减意图与独立 Replay 原子绑定，结算实际调用并推进不可变删除状态。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairTrialUsage
from agentgit.core.repair_studies import (
    RepairReductionIntent,
    RepairReductionReceipt,
    RepairStudyLease,
)
from agentgit.core.repair_validation import ValidationTrialEvidence, validation_ref
from agentgit.core.research import AgentState
from agentgit.core.versions import LiveOverrides, LiveReplay
from agentgit.repair.reduction import record_deletion
from agentgit.services.environment import source_revision
from agentgit.services.jobs import JobService
from agentgit.services.repair_studies import TABLES, RepairStudyService
from agentgit.services.repair_trials import RepairTrialService
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService


class RepairReductionService:
    _usage = staticmethod(RepairTrialService._usage)

    def __init__(self, research: ResearchService, *, fault: Callable[[str], None] | None = None):
        self.research, self.history = research, research.history
        self.ledger, self.replays = RepairStudyService(self.history), ReplayService(research)
        self.fault = fault or (lambda _: None)

    def _trial(
        self, connection: sa.Connection, lease: RepairStudyLease, identity: UUID
    ) -> tuple[Any, RepairReductionIntent]:
        table = TABLES["repair_reduction_trials"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == UUID(lease.repository_id),
                    table.c.study_id == UUID(lease.study_id),
                    table.c.id == identity,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "缩减试验不属于当前控制任务")
        intent = self.ledger.owned(
            connection,
            UUID(lease.repository_id),
            row["intent_ref"],
            "repair_reduction_trial",
            identity,
            "intent",
            RepairReductionIntent,
        )
        if (
            intent.study_id != lease.study_id
            or intent.ordinal != row["ordinal"]
            or intent.protocol_ref != row["protocol_ref"]
            or validation_ref(intent.deletion.after) != row["candidate_ref"]
            or intent.branch_name != row["branch_name"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "缩减意图与索引不同")
        return row, intent

    def _dispatch(self, connection: sa.Connection, identity: UUID) -> Any:
        return (
            connection.execute(
                sa.select(TABLES["repair_reduction_jobs"]).where(
                    TABLES["repair_reduction_jobs"].c.id == identity
                )
            )
            .mappings()
            .first()
        )

    def _result(
        self, connection: sa.Connection, repository: UUID, identity: UUID
    ) -> dict[str, Any] | None:
        table = TABLES["repair_reduction_results"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.repository_id == repository, table.c.id == identity)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        receipt = self.ledger.owned(
            connection,
            repository,
            row["result_ref"],
            "repair_reduction_trial",
            identity,
            "result",
            RepairReductionReceipt,
        )
        if (
            receipt.trial_id != str(identity)
            or receipt.verdict != row["verdict"]
            or receipt.not_started != row["not_started"]
            or any(
                getattr(receipt.usage, n) != row[n]
                for n in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "缩减结果索引与收据不符")
        return {
            "settled": True,
            "reused": True,
            "result_ref": row["result_ref"],
            "result": receipt.model_dump(mode="json"),
        }

    def dispatch(self, lease: RepairStudyLease, identity: UUID) -> dict[str, Any]:
        repo = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            trial, intent = self._trial(connection, lease, identity)
            existing = self._dispatch(connection, identity)
            if existing is not None:
                return {
                    "trial_id": str(identity),
                    "job_id": str(existing["job_id"]),
                    "reused": True,
                }
            if controller["cancel_requested"] or controller["status"] != "reducing":
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消/待核对时不能派发新缩减")
            study = self.ledger._root(connection, repo, UUID(lease.study_id))
            manifest = self.ledger._manifest(connection, study)
            source = self.ledger.screening._manifest(
                connection, self.ledger.screening._root(connection, repo, study["search_id"])
            )
        if manifest.code_revision != source_revision() or intent.limits != source.budget.trial:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "缩减试验代码或预算与冻结条件不同")
        applied = self.ledger._applied(repo, source, intent.deletion.after)
        if applied.replacements and intent.hybrid_ref is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "观测替换不能降级成 Live")
        prepared = self.replays.prepare(
            repo,
            LiveReplay(
                source_run_id=source.space.binding.failed_run_id,
                from_commit=source.space.binding.base_commit,
                branch_name=intent.branch_name,
                budget=intent.limits,
                overrides=LiveOverrides(config=applied.config, plan=applied.plan),
            ),
            hybrid_ref=intent.hybrid_ref,
            repair_reduction_trial_id=identity if intent.hybrid_ref else None,
        )
        if prepared.code_revision != manifest.code_revision:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "缩减准备不能改变冻结代码")
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            self._trial(connection, lease, identity)
            existing = self._dispatch(connection, identity)
            if existing is not None:
                return {
                    "trial_id": str(identity),
                    "job_id": str(existing["job_id"]),
                    "reused": True,
                }
            if (
                controller["cancel_requested"]
                or controller["status"] != "reducing"
                or controller["state_ref"] != intent.protocol_ref
            ):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "准备期间控制状态改变，未派发缩减")
            created = self.replays.publish(
                prepared, actor=study["actor"], key=trial["intent_key"], connection=connection
            )
            self.fault("after_job_created")
            connection.execute(
                TABLES["repair_reduction_jobs"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    job_id=UUID(created["job_id"]),
                    initial_run_id=UUID(created["run_id"]),
                )
            )
            self.ledger._update(
                connection,
                controller,
                "trial_dispatched",
                {"trial_id": str(identity), "job_id": created["job_id"]},
            )
        self.fault("after_dispatch_commit")
        return {"trial_id": str(identity), "job_id": created["job_id"], "reused": False}

    def _waiting(
        self,
        lease: RepairStudyLease,
        identity: UUID,
        reason: str,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repo = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            self._trial(connection, lease, identity)
            saved = self._result(connection, repo, identity)
            if saved is not None:
                return saved
            table = TABLES["repair_reduction_waits"]
            fresh = (
                connection.execute(sa.select(table.c.id).where(table.c.id == identity)).first()
                is None
            )
            if fresh:
                details: dict[str, Any] = {
                    "schema_version": 1,
                    "trial_id": str(identity),
                    "reason": reason,
                }
                if observation is not None:
                    report = self.history.objects.put_json(ObjectType.BLOB, observation)
                    self.history._ref(
                        connection,
                        repo,
                        report,
                        "repair_reduction_trial",
                        str(identity),
                        "waiting_report",
                    )
                    details["observation_ref"] = report.id
                obj = self.history.objects.put_json(
                    ObjectType.BLOB,
                    details,
                )
                self.history._ref(
                    connection, repo, obj, "repair_reduction_trial", str(identity), "waiting"
                )
                connection.execute(
                    table.insert().values(
                        id=identity, repository_id=repo, reason=reason, payload_ref=obj.id
                    )
                )
            status = "cancelling" if controller["cancel_requested"] else "waiting_reconciliation"
            if fresh or controller["status"] != status:
                self.ledger._update(
                    connection,
                    controller,
                    "trial_waiting",
                    {"trial_id": str(identity), "reason": reason},
                    status=status,
                    stop_reason=reason,
                )
        return {"settled": False, "reason": reason}

    def settle(self, lease: RepairStudyLease, identity: UUID) -> dict[str, Any]:
        repo = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            self.ledger._leased(connection, lease)
            _, intent = self._trial(connection, lease, identity)
            saved = self._result(connection, repo, identity)
            if saved is not None:
                return saved
            dispatched = self._dispatch(connection, identity)
            if dispatched is None:
                return {"settled": False, "reason": "not_dispatched"}
            study = self.ledger._root(connection, repo, UUID(lease.study_id))
            manifest = self.ledger._manifest(connection, study)
        job = JobService(self.research).get(repo, dispatched["job_id"])
        if job["status"] not in TERMINAL_RUNS:
            if job["status"] == "outcome_unknown":
                return self._waiting(lease, identity, "outcome_unknown")
            return {"settled": False, "reason": "job_pending"}
        if manifest.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码重判旧缩减")
        report = self.replays.report(repo, dispatched["job_id"])
        if report is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "缩减缺少真实 Replay 报告")
        usage = self._usage(report)
        if usage is None:
            return self._waiting(lease, identity, "physical_usage_unknown", report)
        category, verdict = "execution", "incomplete"
        assessment = None
        safety: tuple[str, ...] = ()
        if report["initialized"]:
            assessment = ValidatorService(self.history).evaluate(
                repo,
                intent.deletion.validator_ref,
                job["run_id"],
                job["checkpoint_commit_id"],
                persist=False,
            )
            state = parse_contract(
                encode(self.history.restore(repo, job["checkpoint_commit_id"]).data), AgentState
            )
            if state.error_code in ("forbidden", "unauthorized"):
                safety = (state.error_code,)
            if (
                assessment.verdict in ("passed", "failed")
                and state.evaluation is not None
                and state.error_code in (None, ErrorCode.VALIDATION_FAILED.value)
                and not safety
            ):
                category, verdict = "oracle", assessment.verdict
        if job["status"] == "budget_exhausted":
            category, verdict = "budget", "incomplete"
        elif job["status"] == "cancelled":
            category, verdict = "cancelled", "incomplete"
        elif safety:
            category, verdict = "safety", "incomplete"
        over = any(
            getattr(usage, name) > getattr(intent.limits, limit)
            for name, limit in (
                ("model_calls", "max_model_calls"),
                ("tool_calls", "max_tool_calls"),
                ("tokens", "max_tokens"),
                ("cost_micro_usd", "max_cost_micro_usd"),
            )
        )
        if over:
            category, verdict = "budget", "incomplete"
        report_obj = self.history.objects.put_json(ObjectType.BLOB, report)
        receipt = RepairReductionReceipt.model_validate(
            dict(
                study_id=lease.study_id,
                trial_id=str(identity),
                job_id=str(dispatched["job_id"]),
                initial_run_id=str(dispatched["initial_run_id"]),
                current_run_id=str(job["run_id"]),
                protocol_ref=intent.protocol_ref,
                candidate_ref=validation_ref(intent.deletion.after),
                sample_ref=intent.deletion.sample_ref,
                validator_ref=intent.deletion.validator_ref,
                verdict=verdict,
                category=category,
                usage=usage,
                replay_report_ref=report_obj.id,
                assessment=assessment,
                safety_violations=safety,
                uncertainties=("observed_usage_exceeds_reservation",) if over else (),
            )
        )
        obj = self.history.objects.put_json(ObjectType.BLOB, receipt.model_dump(mode="json"))
        evidence = ValidationTrialEvidence.model_validate(
            dict(
                phase="reduce",
                protocol_ref=intent.protocol_ref,
                trial_id=str(identity),
                run_id=str(job["run_id"]),
                result_ref=obj.id,
                candidate_ref=receipt.candidate_ref,
                sample_ref=receipt.sample_ref,
                validator_ref=receipt.validator_ref,
                verdict=verdict,
                category=category,
                usage=usage,
                safety_violations=safety,
            )
        )
        evidence_obj = self.history.objects.put_json(
            ObjectType.BLOB, evidence.model_dump(mode="json")
        )
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            saved = self._result(connection, repo, identity)
            if saved is not None:
                return saved
            if controller["state_ref"] != intent.protocol_ref:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "缩减前置状态已经改变")
            current = (
                connection.execute(
                    sa.select(TABLES["jobs"])
                    .where(TABLES["jobs"].c.id == dispatched["job_id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if any(
                current[key] != job[key]
                for key in ("run_id", "status", "checkpoint_commit_id", "attempt", "fencing_token")
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "缩减研究 Job 在结算前变化")
            updated = record_deletion(self.ledger._state(connection, controller), evidence)
            snapshot = self.history.objects.put_json(
                ObjectType.BLOB, updated.model_dump(mode="json")
            )
            for role, body in (
                ("result", obj),
                ("evidence", evidence_obj),
                ("replay_report", report_obj),
            ):
                self.history._ref(
                    connection, repo, body, "repair_reduction_trial", str(identity), role
                )
            self.history._ref(
                connection, repo, snapshot, "repair_study", lease.study_id, "reduction_state"
            )
            connection.execute(
                TABLES["repair_reduction_results"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    result_ref=obj.id,
                    evidence_ref=evidence_obj.id,
                    not_started=False,
                    verdict=verdict,
                    **{
                        n: getattr(usage, n)
                        for n in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                    },
                )
            )
            self.fault("before_result_commit")
            status = "cancelling" if controller["cancel_requested"] else "reducing"
            values: dict[str, Any] = dict(state_ref=snapshot.id, status=status, stop_reason=None)
            if controller["cancel_requested"]:
                values.update(
                    status="cancelled",
                    stop_reason="operator_cancelled",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            if over or safety:
                values.update(
                    status="needs_attention",
                    stop_reason="usage_above_reservation" if over else "safety_violation",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            self.ledger._update(
                connection,
                controller,
                "trial_settled",
                {"trial_id": str(identity), "result_ref": obj.id, "state_ref": snapshot.id},
                **values,
            )
        self.fault("after_result_commit")
        return {
            "settled": True,
            "reused": False,
            "result_ref": obj.id,
            "result": receipt.model_dump(mode="json"),
        }

    def cancel_children(self, lease: RepairStudyLease) -> None:
        repo = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            if not controller["cancel_requested"]:
                raise AgentGitError(ErrorCode.FORBIDDEN, "没有持久取消请求")
            study = self.ledger._root(connection, repo, UUID(lease.study_id))
            provider = (
                self.ledger._manifest(connection, study).initial_state.initial.model_scope
                == "provider"
            )
            trials = (
                connection.execute(
                    sa.select(TABLES["repair_reduction_trials"]).where(
                        TABLES["repair_reduction_trials"].c.study_id == UUID(lease.study_id)
                    )
                )
                .mappings()
                .all()
            )
            for trial in trials:
                if self._result(connection, repo, trial["id"]) is not None:
                    continue
                dispatched = self._dispatch(connection, trial["id"])
                if dispatched is not None:
                    jobs, runs = TABLES["jobs"], TABLES["runs"]
                    run = (
                        connection.execute(
                            sa.select(runs)
                            .join(jobs, jobs.c.run_id == runs.c.id)
                            .where(jobs.c.id == dispatched["job_id"])
                        )
                        .mappings()
                        .one()
                    )
                    if run["status"] not in TERMINAL_RUNS and run["control_status"] != "requested":
                        JobService(self.research).request_control(
                            repo,
                            run["id"],
                            "cancel",
                            expected_version=run["state_version"],
                            connection=connection,
                        )
                    continue
                _, intent = self._trial(connection, lease, trial["id"])
                receipt = RepairReductionReceipt(
                    study_id=lease.study_id,
                    trial_id=str(trial["id"]),
                    job_id=None,
                    initial_run_id=None,
                    current_run_id=None,
                    protocol_ref=intent.protocol_ref,
                    candidate_ref=validation_ref(intent.deletion.after),
                    sample_ref=intent.deletion.sample_ref,
                    validator_ref=intent.deletion.validator_ref,
                    verdict="incomplete",
                    category="cancelled",
                    usage=RepairTrialUsage(
                        model_calls=0,
                        tool_calls=0,
                        tokens=0,
                        cost_micro_usd=0,
                        unit="provider_tokens" if provider else "mock_utf8_bytes",
                        cost_basis="pricebook_estimate" if provider else "local_mock_free",
                    ),
                    not_started=True,
                    replay_report_ref=None,
                )
                obj = self.history.objects.put_json(
                    ObjectType.BLOB, receipt.model_dump(mode="json")
                )
                self.history._ref(
                    connection, repo, obj, "repair_reduction_trial", str(trial["id"]), "result"
                )
                connection.execute(
                    TABLES["repair_reduction_results"]
                    .insert()
                    .values(
                        id=trial["id"],
                        repository_id=repo,
                        result_ref=obj.id,
                        evidence_ref=None,
                        not_started=True,
                        verdict="incomplete",
                        model_calls=0,
                        tool_calls=0,
                        tokens=0,
                        cost_micro_usd=0,
                    )
                )
            study = self.ledger._root(connection, repo, UUID(lease.study_id))
            values = (
                dict(
                    status="cancelled",
                    stop_reason="operator_cancelled",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
                if not self.ledger._consumption(connection, study).active
                else {}
            )
            self.ledger._update(connection, controller, "children_cancelled", {}, **values)
