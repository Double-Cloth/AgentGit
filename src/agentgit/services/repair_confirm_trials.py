"""验证意图的独立 Replay 原子派发、实际结算与取消核对。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairTrialUsage
from agentgit.core.repair_confirmation import RepairValidationReceipt
from agentgit.core.repair_studies import RepairStudyLease
from agentgit.core.repair_validation import validation_ref
from agentgit.core.research import AgentState
from agentgit.core.versions import LiveOverrides, LiveReplay
from agentgit.services.environment import source_revision
from agentgit.services.jobs import JobService
from agentgit.services.repair_confirmation import TABLES, RepairConfirmationService
from agentgit.services.repair_trials import RepairTrialService
from agentgit.services.repair_validation_inputs import execution_proof
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService


class RepairConfirmationTrialService:
    _usage = staticmethod(RepairTrialService._usage)

    def __init__(self, research: ResearchService, *, fault: Callable[[str], None] | None = None):
        self.research, self.history = research, research.history
        self.control, self.replays = RepairConfirmationService(research), ReplayService(research)
        self.ledger = self.control.ledger
        self.fault = fault or (lambda _: None)

    @staticmethod
    def _settled(value: Any) -> dict[str, Any]:
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
            row, intent = self.control._trial(connection, repo, study, identity)
            found = self.control._dispatch(connection, identity)
            if found is not None:
                return dict(trial_id=str(identity), job_id=str(found["job_id"]), reused=True)
            if job["cancel_requested"] or job["status"] not in ("confirming", "regressing"):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消或待核对时不能派发验证")
            projection = self.control._projection(connection, repo, identity, intent)
            root = self.ledger._root(connection, repo, study)
        ticket = intent.registered.ticket
        if ticket.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码派发旧采样票据")
        prepared = self.replays.prepare(
            repo,
            LiveReplay(
                source_run_id=ticket.source_run_id,
                from_commit=ticket.from_commit,
                branch_name=intent.branch_name,
                budget=intent.limits,
                overrides=LiveOverrides(config=projection.config, plan=projection.plan),
            ),
            hybrid_ref=projection.hybrid_ref,
            repair_validation_trial_id=identity if projection.hybrid_ref else None,
        )
        if prepared.code_revision != ticket.code_revision or prepared.config != projection.config:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Replay 准备不能更换冻结验证配置")
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            self.control._trial(connection, repo, study, identity)
            found = self.control._dispatch(connection, identity)
            if found is not None:
                return dict(trial_id=str(identity), job_id=str(found["job_id"]), reused=True)
            if job["cancel_requested"] or job["status"] not in ("confirming", "regressing"):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "准备期间控制改变，未创建验证 Job")
            submitted = self.replays.publish(
                prepared, actor=root["actor"], key=row["intent_key"], connection=connection
            )
            self.fault("after_job_created")
            connection.execute(
                TABLES["repair_validation_jobs"]
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
                "validation_dispatched",
                dict(trial_id=str(identity), job_id=submitted["job_id"]),
            )
        self.fault("after_dispatch_commit")
        return dict(trial_id=str(identity), job_id=submitted["job_id"], reused=False)

    def _waiting(
        self,
        lease: RepairStudyLease,
        identity: UUID,
        reason: str,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            self.control._trial(connection, repo, study, identity)
            found = self.control._result(connection, repo, identity)
            if found is not None:
                return self._settled(found)
            table = TABLES["repair_validation_waits"]
            fresh = (
                connection.execute(sa.select(table.c.id).where(table.c.id == identity)).first()
                is None
            )
            if fresh:
                details = dict(schema_version=1, reason=reason, trial_id=str(identity))
                if report is not None:
                    observation = self.history.objects.put_json(ObjectType.BLOB, report)
                    self.history._ref(
                        connection,
                        repo,
                        observation,
                        "repair_validation_trial",
                        str(identity),
                        "waiting_report",
                    )
                    details["observation_ref"] = observation.id
                body = self.history.objects.put_json(ObjectType.BLOB, details)
                self.history._ref(
                    connection, repo, body, "repair_validation_trial", str(identity), "waiting"
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
                    "validation_waiting",
                    dict(trial_id=str(identity), reason=reason),
                    status=status,
                    stop_reason=reason,
                )
        return dict(settled=False, reason=reason)

    def settle(self, lease: RepairStudyLease, identity: UUID) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            self.ledger._leased(connection, lease)
            _, intent = self.control._trial(connection, repo, study, identity)
            saved = self.control._result(connection, repo, identity)
            if saved is not None:
                return self._settled(saved)
            dispatch = self.control._dispatch(connection, identity)
            if dispatch is None:
                return dict(settled=False, reason="not_dispatched")
            projection = self.control._projection(connection, repo, identity, intent)
        job = JobService(self.research).get(repo, dispatch["job_id"])
        if job["status"] not in TERMINAL_RUNS:
            if job["status"] == "outcome_unknown":
                return self._waiting(lease, identity, "outcome_unknown")
            return dict(settled=False, reason="job_pending")
        if intent.registered.ticket.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码重判旧验证试验")
        report = self.replays.report(repo, dispatch["job_id"])
        if report is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "验证 Job 缺少 Replay 报告")
        usage = self._usage(report)
        if usage is None:
            return self._waiting(lease, identity, "physical_usage_unknown", report)
        proof = execution_proof(
            self.research, repo, identity, intent, projection, job, dispatch["initial_run_id"]
        )
        fresh = sum(call.origin_call_id is None for call in proof.model_calls)
        if fresh != usage.model_calls:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "实际模型用量与原始调用来源不一致")
        assessment = None
        safety: tuple[str, ...] = ()
        category, verdict = "execution", "incomplete"
        if report["initialized"]:
            assessment = ValidatorService(self.history).evaluate(
                repo,
                intent.registered.sample.validator_ref,
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
        uncertainty = []
        if over:
            category, verdict = "budget", "incomplete"
            uncertainty.append("observed_usage_exceeds_reservation")
        if report["actual"]["model_usage"]["provider"]["estimated_usage_calls"]:
            uncertainty.append("provider_usage_conservatively_estimated")
        report_obj = self.history.objects.put_json(ObjectType.BLOB, report)
        proof_obj = self.history.objects.put_json(ObjectType.BLOB, proof.model_dump(mode="json"))
        receipt = RepairValidationReceipt.model_validate(
            dict(
                study_id=str(study),
                trial_id=str(identity),
                sample_id=intent.sample_id,
                phase=intent.phase,
                job_id=str(dispatch["job_id"]),
                initial_run_id=str(dispatch["initial_run_id"]),
                current_run_id=str(job["run_id"]),
                protocol_ref=intent.protocol_ref,
                candidate_ref=intent.lock_ref,
                sample_ref=validation_ref(intent.registered.sample),
                validator_ref=intent.registered.sample.validator_ref,
                projection_ref=intent.projection_ref,
                verdict=verdict,
                category=category,
                usage=usage,
                replay_report_ref=report_obj.id,
                provenance_ref=proof_obj.id,
                fresh_model_calls=fresh,
                assessment=assessment,
                safety_violations=safety,
                uncertainties=tuple(uncertainty),
            )
        )
        result_obj = self.history.objects.put_json(ObjectType.BLOB, receipt.model_dump(mode="json"))
        evidence = self.control.evidence(receipt, result_obj.id)
        evidence_obj = self.history.objects.put_json(
            ObjectType.BLOB, evidence.model_dump(mode="json")
        )
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            saved = self.control._result(connection, repo, identity)
            if saved is not None:
                return self._settled(saved)
            current = (
                connection.execute(
                    sa.select(TABLES["jobs"])
                    .where(TABLES["jobs"].c.id == dispatch["job_id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if any(
                current[name] != job[name]
                for name in ("run_id", "status", "checkpoint_commit_id", "attempt", "fencing_token")
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "验证 Job 在结算准备期间改变")
            for role, body in (
                ("result", result_obj),
                ("evidence", evidence_obj),
                ("replay_report", report_obj),
                ("provenance", proof_obj),
            ):
                self.history._ref(
                    connection, repo, body, "repair_validation_trial", str(identity), role
                )
            connection.execute(
                TABLES["repair_validation_results"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    result_ref=result_obj.id,
                    evidence_ref=evidence_obj.id,
                    not_started=False,
                    verdict=receipt.verdict,
                    **{
                        name: getattr(usage, name)
                        for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                    },
                )
            )
            self.fault("before_result_commit")
            status = (
                "cancelling"
                if controller["cancel_requested"]
                else "confirming"
                if intent.ordinal < 5
                else "regressing"
            )
            self.ledger._update(
                connection,
                controller,
                "validation_settled",
                dict(trial_id=str(identity), result_ref=result_obj.id),
                status=status,
                stop_reason=None,
            )
        self.fault("after_result_commit")
        return dict(
            settled=True,
            reused=False,
            result_ref=result_obj.id,
            result=receipt.model_dump(mode="json"),
        )

    def cancel_children(self, lease: RepairStudyLease) -> None:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            if not controller["cancel_requested"]:
                raise AgentGitError(ErrorCode.FORBIDDEN, "验证取消需要持久控制请求")
            table = TABLES["repair_validation_trials"]
            identities = (
                connection.execute(sa.select(table.c.id).where(table.c.study_id == study))
                .scalars()
                .all()
            )
            for identity in identities:
                if self.control._result(connection, repo, identity) is not None:
                    continue
                _, intent = self.control._trial(connection, repo, study, identity)
                dispatch = self.control._dispatch(connection, identity)
                if dispatch is not None:
                    jobs, runs = TABLES["jobs"], TABLES["runs"]
                    run = (
                        connection.execute(
                            sa.select(runs)
                            .join(jobs, jobs.c.run_id == runs.c.id)
                            .where(jobs.c.id == dispatch["job_id"])
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
                provider = (
                    self.control._projection(connection, repo, identity, intent).config.openai
                    is not None
                )
                receipt = RepairValidationReceipt(
                    study_id=str(study),
                    trial_id=str(identity),
                    sample_id=intent.sample_id,
                    phase=intent.phase,
                    job_id=None,
                    initial_run_id=None,
                    current_run_id=None,
                    protocol_ref=intent.protocol_ref,
                    candidate_ref=intent.lock_ref,
                    sample_ref=validation_ref(intent.registered.sample),
                    validator_ref=intent.registered.sample.validator_ref,
                    projection_ref=intent.projection_ref,
                    verdict="incomplete",
                    category="cancelled",
                    not_started=True,
                    usage=RepairTrialUsage(
                        model_calls=0,
                        tool_calls=0,
                        tokens=0,
                        cost_micro_usd=0,
                        unit="provider_tokens" if provider else "mock_utf8_bytes",
                        cost_basis="pricebook_estimate" if provider else "local_mock_free",
                    ),
                    replay_report_ref=None,
                    provenance_ref=None,
                    fresh_model_calls=0,
                )
                body = self.history.objects.put_json(
                    ObjectType.BLOB, receipt.model_dump(mode="json")
                )
                self.history._ref(
                    connection, repo, body, "repair_validation_trial", str(identity), "result"
                )
                connection.execute(
                    TABLES["repair_validation_results"]
                    .insert()
                    .values(
                        id=identity,
                        repository_id=repo,
                        result_ref=body.id,
                        evidence_ref=None,
                        not_started=True,
                        verdict="incomplete",
                        model_calls=0,
                        tool_calls=0,
                        tokens=0,
                        cost_micro_usd=0,
                    )
                )
            self.ledger._update(connection, controller, "validation_cancel_children", {})
