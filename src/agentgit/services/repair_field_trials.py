"""字段试验的原子派发、物理收据、恢复来源及取消。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairTrialUsage
from agentgit.core.repair_fields import RepairFieldReceipt
from agentgit.core.repair_studies import RepairReductionReceipt, RepairStudyLease
from agentgit.core.repair_validation import validation_ref
from agentgit.core.research import AgentState
from agentgit.core.versions import LiveOverrides, LiveReplay
from agentgit.repair.field_reduction import record_field_trial
from agentgit.services.environment import source_revision
from agentgit.services.jobs import JobService
from agentgit.services.repair_field_provenance import field_execution_proof
from agentgit.services.repair_fields import TABLES, RepairFieldService
from agentgit.services.repair_trials import RepairTrialService
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService


class RepairFieldTrialService:
    _usage = staticmethod(RepairTrialService._usage)

    def __init__(self, research: ResearchService, *, fault: Callable[[str], None] | None = None):
        self.research, self.history = research, research.history
        self.fields, self.replays = RepairFieldService(self.history), ReplayService(research)
        self.ledger = self.fields.ledger
        self.fault = fault or (lambda _: None)

    @staticmethod
    def settled(value: Any) -> dict[str, Any]:
        row, receipt = value
        return dict(
            settled=True,
            reused=True,
            result_ref=row["result_ref"],
            result=receipt.model_dump(mode="json"),
        )

    def dispatch(self, lease: RepairStudyLease, identity: UUID) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            row, intent = self.fields.trial(connection, repo, study, identity)
            previous = self.fields.dispatch_row(connection, identity)
            if previous is not None:
                return dict(trial_id=str(identity), job_id=str(previous["job_id"]), reused=True)
            if (
                job["cancel_requested"]
                or job["status"] != "reducing"
                or job["field_state_ref"] != intent.protocol_ref
            ):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消或协议改变时不能派发字段")
            source, root = (
                self.fields.source(connection, repo, study),
                self.ledger._root(connection, repo, study),
            )
        if source.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码派发旧字段试验")
        prepared = self.replays.prepare(
            repo,
            LiveReplay(
                source_run_id=source.space.binding.failed_run_id,
                from_commit=source.space.binding.base_commit,
                branch_name=intent.branch_name,
                budget=intent.limits,
                overrides=LiveOverrides(config=intent.config, plan=intent.plan),
            ),
            hybrid_ref=intent.hybrid_ref,
            repair_field_trial_id=identity if intent.hybrid_ref else None,
        )
        if prepared.code_revision != source.code_revision or prepared.config != intent.config:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "字段 Replay 准备改变了冻结条件")
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            self.fields.trial(connection, repo, study, identity)
            previous = self.fields.dispatch_row(connection, identity)
            if previous is not None:
                return dict(trial_id=str(identity), job_id=str(previous["job_id"]), reused=True)
            if (
                job["cancel_requested"]
                or job["status"] != "reducing"
                or job["field_state_ref"] != intent.protocol_ref
            ):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "准备期间字段控制改变，未创建 Job")
            submitted = self.replays.publish(
                prepared, actor=root["actor"], key=row["intent_key"], connection=connection
            )
            self.fault("after_job_created")
            connection.execute(
                TABLES["repair_field_jobs"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    job_id=UUID(submitted["job_id"]),
                    initial_run_id=UUID(submitted["run_id"]),
                )
            )
            self.ledger._update(
                connection,
                job,
                "field_trial_dispatched",
                dict(trial_id=str(identity), job_id=submitted["job_id"]),
            )
        self.fault("after_dispatch_commit")
        return dict(trial_id=str(identity), job_id=submitted["job_id"], reused=False)

    def waiting(
        self,
        lease: RepairStudyLease,
        identity: UUID,
        reason: str,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            self.fields.trial(connection, repo, study, identity)
            saved = self.fields.result(connection, repo, identity)
            if saved is not None:
                return self.settled(saved)
            table = TABLES["repair_field_waits"]
            fresh = (
                connection.execute(sa.select(table.c.id).where(table.c.id == identity)).first()
                is None
            )
            if fresh:
                details = dict(schema_version=1, trial_id=str(identity), reason=reason)
                if report is not None:
                    observed = self.history.objects.put_json(ObjectType.BLOB, report)
                    self.history._ref(
                        connection,
                        repo,
                        observed,
                        "repair_field_trial",
                        str(identity),
                        "waiting_report",
                    )
                    details["observation_ref"] = observed.id
                body = self.history.objects.put_json(ObjectType.BLOB, details)
                self.history._ref(
                    connection, repo, body, "repair_field_trial", str(identity), "waiting"
                )
                connection.execute(
                    table.insert().values(
                        id=identity, repository_id=repo, reason=reason, payload_ref=body.id
                    )
                )
            status = "cancelling" if job["cancel_requested"] else "waiting_reconciliation"
            if fresh or job["status"] != status:
                self.ledger._update(
                    connection,
                    job,
                    "field_trial_waiting",
                    dict(trial_id=str(identity), reason=reason),
                    status=status,
                    stop_reason=reason,
                )
        return dict(settled=False, reason=reason)

    def settle(self, lease: RepairStudyLease, identity: UUID) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            self.ledger._leased(connection, lease)
            _, intent = self.fields.trial(connection, repo, study, identity)
            saved = self.fields.result(connection, repo, identity)
            if saved is not None:
                return self.settled(saved)
            dispatched = self.fields.dispatch_row(connection, identity)
            if dispatched is None:
                return dict(settled=False, reason="not_dispatched")
            source = self.fields.source(connection, repo, study)
        job = JobService(self.research).get(repo, dispatched["job_id"])
        if job["status"] not in TERMINAL_RUNS:
            return (
                self.waiting(lease, identity, "outcome_unknown")
                if job["status"] == "outcome_unknown"
                else dict(settled=False, reason="job_pending")
            )
        if source.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码重判旧字段试验")
        report = self.replays.report(repo, dispatched["job_id"])
        if report is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段实际 Job 缺少 Replay 报告")
        usage = self._usage(report)
        if usage is None:
            return self.waiting(lease, identity, "physical_usage_unknown", report)
        proof = field_execution_proof(
            self.research, repo, identity, source, intent, job, dispatched["initial_run_id"]
        )
        fresh = sum(call.origin_call_id is None for call in proof.model_calls)
        if fresh != usage.model_calls:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "字段新模型调用与物理计量不同")
        category, verdict = "execution", "incomplete"
        assessment = None
        safety: tuple[str, ...] = ()
        if report["initialized"]:
            assessment = ValidatorService(self.history).evaluate(
                repo,
                intent.request.validator_ref,
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
                fresh
                and assessment.verdict in ("passed", "failed")
                and state.evaluation is not None
                and state.error_code in (None, ErrorCode.VALIDATION_FAILED.value)
                and not safety
            ):
                category, verdict = "oracle", assessment.verdict
        if job["status"] == "cancelled":
            category, verdict = "cancelled", "incomplete"
        elif job["status"] == "budget_exhausted":
            category, verdict = "budget", "incomplete"
        elif safety:
            category, verdict = "safety", "incomplete"
        over = any(
            getattr(usage, name) > getattr(intent.limits, cap)
            for name, cap in (
                ("model_calls", "max_model_calls"),
                ("tool_calls", "max_tool_calls"),
                ("tokens", "max_tokens"),
                ("cost_micro_usd", "max_cost_micro_usd"),
            )
        )
        uncertainties = []
        if over:
            category, verdict = "budget", "incomplete"
            uncertainties.append("observed_usage_exceeds_reservation")
        if report["actual"]["model_usage"]["provider"]["estimated_usage_calls"]:
            uncertainties.append("provider_usage_conservatively_estimated")
        report_obj = self.history.objects.put_json(ObjectType.BLOB, report)
        proof_obj = self.history.objects.put_json(ObjectType.BLOB, proof.model_dump(mode="json"))
        receipt = RepairFieldReceipt(
            execution=RepairReductionReceipt.model_validate(
                dict(
                    study_id=str(study),
                    trial_id=str(identity),
                    job_id=str(dispatched["job_id"]),
                    initial_run_id=str(dispatched["initial_run_id"]),
                    current_run_id=str(job["run_id"]),
                    protocol_ref=intent.protocol_ref,
                    candidate_ref=validation_ref(intent.request.after),
                    sample_ref=intent.request.sample_ref,
                    validator_ref=intent.request.validator_ref,
                    verdict=verdict,
                    category=category,
                    usage=usage,
                    replay_report_ref=report_obj.id,
                    assessment=assessment,
                    safety_violations=safety,
                    uncertainties=tuple(uncertainties),
                )
            ),
            provenance_ref=proof_obj.id,
            fresh_model_calls=fresh,
        )
        body = self.history.objects.put_json(ObjectType.BLOB, receipt.model_dump(mode="json"))
        evidence = self.fields.evidence(receipt, body.id)
        evidence_obj = self.history.objects.put_json(
            ObjectType.BLOB, evidence.model_dump(mode="json")
        )
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            saved = self.fields.result(connection, repo, identity)
            if saved is not None:
                return self.settled(saved)
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
                current[name] != job[name]
                for name in ("run_id", "status", "checkpoint_commit_id", "attempt", "fencing_token")
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "字段 Job 在结算准备期间改变")
            state_value = self.fields.state(connection, repo, study, controller)
            if state_value is None or validation_ref(state_value) != intent.protocol_ref:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "字段协议在结算前改变")
            updated = record_field_trial(state_value, evidence)
            for role, obj in (
                ("result", body),
                ("evidence", evidence_obj),
                ("provenance", proof_obj),
                ("replay_report", report_obj),
            ):
                self.history._ref(connection, repo, obj, "repair_field_trial", str(identity), role)
            connection.execute(
                TABLES["repair_field_results"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    result_ref=body.id,
                    evidence_ref=evidence_obj.id,
                    provenance_ref=proof_obj.id,
                    not_started=False,
                    verdict=verdict,
                    **{
                        name: getattr(usage, name)
                        for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                    },
                )
            )
            self.fault("before_result_commit")
            updates: dict[str, Any] = dict(
                status="cancelling" if controller["cancel_requested"] else "reducing",
                stop_reason=None,
            )
            if over or safety:
                updates.update(
                    status="needs_attention",
                    stop_reason="usage_above_reservation" if over else "safety_violation",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            self.fields.advance(
                connection,
                repo,
                study,
                controller,
                updated,
                "field_trial_settled",
                dict(trial_id=str(identity), result_ref=body.id),
                **updates,
            )
        self.fault("after_result_commit")
        return dict(
            settled=True, reused=False, result_ref=body.id, result=receipt.model_dump(mode="json")
        )

    def cancel_children(self, lease: RepairStudyLease) -> None:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            if not controller["cancel_requested"]:
                raise AgentGitError(ErrorCode.FORBIDDEN, "字段取消需要持久控制请求")
            table = TABLES["repair_field_trials"]
            ids = (
                connection.execute(sa.select(table.c.id).where(table.c.study_id == study))
                .scalars()
                .all()
            )
            for identity in ids:
                if self.fields.result(connection, repo, identity) is not None:
                    continue
                _, intent = self.fields.trial(connection, repo, study, identity)
                dispatched = self.fields.dispatch_row(connection, identity)
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
                provider = intent.config.openai is not None
                receipt = RepairFieldReceipt(
                    execution=RepairReductionReceipt(
                        study_id=str(study),
                        trial_id=str(identity),
                        job_id=None,
                        initial_run_id=None,
                        current_run_id=None,
                        protocol_ref=intent.protocol_ref,
                        candidate_ref=validation_ref(intent.request.after),
                        sample_ref=intent.request.sample_ref,
                        validator_ref=intent.request.validator_ref,
                        verdict="incomplete",
                        category="cancelled",
                        not_started=True,
                        replay_report_ref=None,
                        usage=RepairTrialUsage(
                            model_calls=0,
                            tool_calls=0,
                            tokens=0,
                            cost_micro_usd=0,
                            unit="provider_tokens" if provider else "mock_utf8_bytes",
                            cost_basis="pricebook_estimate" if provider else "local_mock_free",
                        ),
                    ),
                    provenance_ref=None,
                    fresh_model_calls=0,
                )
                body = self.history.objects.put_json(
                    ObjectType.BLOB, receipt.model_dump(mode="json")
                )
                self.history._ref(
                    connection, repo, body, "repair_field_trial", str(identity), "result"
                )
                connection.execute(
                    TABLES["repair_field_results"]
                    .insert()
                    .values(
                        id=identity,
                        repository_id=repo,
                        result_ref=body.id,
                        evidence_ref=None,
                        provenance_ref=None,
                        not_started=True,
                        verdict="incomplete",
                        model_calls=0,
                        tool_calls=0,
                        tokens=0,
                        cost_micro_usd=0,
                    )
                )
            root = self.ledger._root(connection, repo, study)
            updates = (
                dict(
                    status="cancelled",
                    stop_reason="operator_cancelled",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
                if not self.ledger._consumption(connection, root).active
                else {}
            )
            self.ledger._update(connection, controller, "field_children_cancelled", {}, **updates)
