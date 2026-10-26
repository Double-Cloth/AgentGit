"""独立确认/回归控制、共享预算及封存摘要；实际执行由普通 Worker 完成。"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_confirmation import (
    RepairValidationIntent,
    RepairValidationProjection,
    RepairValidationReceipt,
    ValidationExecutionProof,
)
from agentgit.core.repair_pools import RepairValidationOpening
from agentgit.core.repair_studies import RepairStudyLease
from agentgit.core.repair_validation import (
    RepairVerificationSummary,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import evolve
from agentgit.repair.budget import admission
from agentgit.repair.verification import verify_repair
from agentgit.services.environment import source_revision
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.repair_validation_inputs import prepare_projection
from agentgit.services.research import ResearchService
from agentgit.storage.schema import build_metadata
from agentgit.storage.schema_s10_confirmation import STUDY_TERMINAL

TABLES = build_metadata().tables


class RepairConfirmationService:
    def __init__(self, research: ResearchService, *, lease_ms: int = 120_000):
        self.research, self.history = research, research.history
        self.ledger = RepairStudyService(self.history, lease_ms=lease_ms)
        self.pools = RepairValidationPoolService(research)

    def _opening(
        self, connection: sa.Connection, repo: UUID, study: UUID
    ) -> tuple[Any, RepairValidationOpening]:
        row = self.pools._opening(connection, repo, study)
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "确认池尚未开启")
        value = self.ledger.owned(
            connection,
            repo,
            row["opening_ref"],
            "repair_study",
            study,
            "validation_opening",
            RepairValidationOpening,
        )
        if (
            value.study_id != str(study)
            or value.suite_id != str(row["suite_id"])
            or validation_ref(value.plan) != row["plan_ref"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "确认控制与已开启计划不同")
        return row, value

    def _source(self, connection: sa.Connection, repo: UUID, study: UUID) -> Any:
        root = self.ledger._root(connection, repo, study)
        return self.ledger.screening._manifest(
            connection, self.ledger.screening._root(connection, repo, root["search_id"])
        )

    def _trial(
        self, connection: sa.Connection, repo: UUID, study: UUID, identity: UUID
    ) -> tuple[Any, RepairValidationIntent]:
        table = TABLES["repair_validation_trials"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repo, table.c.study_id == study, table.c.id == identity
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "确认/回归意图不可访问")
        intent = self.ledger.owned(
            connection,
            repo,
            row["intent_ref"],
            "repair_validation_trial",
            identity,
            "intent",
            RepairValidationIntent,
        )
        if (
            intent.study_id != str(study)
            or intent.ordinal != row["ordinal"]
            or intent.phase != row["phase"]
            or intent.sample_id != str(row["sample_id"])
            or intent.protocol_ref != row["protocol_ref"]
            or intent.projection_ref != row["projection_ref"]
            or intent.branch_name != row["branch_name"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "确认意图索引与冻结内容不同")
        return row, intent

    def _dispatch(self, connection: sa.Connection, identity: UUID) -> Any:
        table = TABLES["repair_validation_jobs"]
        return connection.execute(sa.select(table).where(table.c.id == identity)).mappings().first()

    def _projection(
        self, connection: sa.Connection, repo: UUID, identity: UUID, intent: RepairValidationIntent
    ) -> RepairValidationProjection:
        value = self.ledger.owned(
            connection,
            repo,
            intent.projection_ref,
            "repair_validation_trial",
            identity,
            "projection",
            RepairValidationProjection,
        )
        if (
            value.lock_ref != intent.lock_ref
            or value.ticket_ref != validation_ref(intent.registered.ticket)
            or value.config.limits != intent.limits
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "验证投影不能改变候选或采样授权")
        return value

    def _result(
        self, connection: sa.Connection, repo: UUID, identity: UUID
    ) -> tuple[Any, RepairValidationReceipt] | None:
        table = TABLES["repair_validation_results"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.repository_id == repo, table.c.id == identity)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        receipt = self.ledger.owned(
            connection,
            repo,
            row["result_ref"],
            "repair_validation_trial",
            identity,
            "result",
            RepairValidationReceipt,
        )
        if (
            receipt.trial_id != str(identity)
            or receipt.verdict != row["verdict"]
            or receipt.not_started != row["not_started"]
            or any(
                getattr(receipt.usage, name) != row[name]
                for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "确认物理收据与索引不同")
        return row, receipt

    def _records(
        self, connection: sa.Connection, repo: UUID, study: UUID
    ) -> tuple[list[dict[str, Any]], tuple[ValidationTrialEvidence, ...]]:
        table = TABLES["repair_validation_trials"]
        ids = (
            connection.execute(
                sa.select(table.c.id)
                .where(table.c.repository_id == repo, table.c.study_id == study)
                .order_by(table.c.ordinal)
            )
            .scalars()
            .all()
        )
        details, evidence = [], []
        for identity in ids:
            row, intent = self._trial(connection, repo, study, identity)
            projection = self._projection(connection, repo, identity, intent)
            detail: dict[str, Any] = dict(
                trial_id=str(identity),
                intent_ref=row["intent_ref"],
                intent=intent.model_dump(mode="json"),
                projection=projection.model_dump(mode="json"),
            )
            dispatched, result = (
                self._dispatch(connection, identity),
                self._result(connection, repo, identity),
            )
            if dispatched is not None:
                detail["dispatch"] = dict(
                    job_id=str(dispatched["job_id"]),
                    initial_run_id=str(dispatched["initial_run_id"]),
                )
            if result is not None:
                saved, receipt = result
                if (
                    receipt.study_id != str(study)
                    or receipt.sample_id != intent.sample_id
                    or receipt.phase != intent.phase
                    or receipt.protocol_ref != intent.protocol_ref
                    or receipt.projection_ref != intent.projection_ref
                    or receipt.candidate_ref != intent.lock_ref
                    or receipt.sample_ref != validation_ref(intent.registered.sample)
                    or receipt.validator_ref != intent.registered.sample.validator_ref
                ):
                    raise AgentGitError(
                        ErrorCode.OBJECT_CORRUPT, "确认收据不能更换候选、样本、计划或验收器"
                    )
                detail["result"] = {
                    **receipt.model_dump(mode="json"),
                    "result_ref": saved["result_ref"],
                    "evidence_ref": saved["evidence_ref"],
                }
                if receipt.not_started:
                    if dispatched is not None or saved["evidence_ref"] is not None:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "未启动意图不应拥有执行证据")
                else:
                    if (
                        dispatched is None
                        or receipt.job_id != str(dispatched["job_id"])
                        or receipt.initial_run_id != str(dispatched["initial_run_id"])
                    ):
                        raise AgentGitError(
                            ErrorCode.OBJECT_CORRUPT, "实际验证收据缺少独立 Job 绑定"
                        )
                    if receipt.provenance_ref is None:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实际确认缺少原始调用证明")
                    proof = self.ledger.owned(
                        connection,
                        repo,
                        receipt.provenance_ref,
                        "repair_validation_trial",
                        identity,
                        "provenance",
                        ValidationExecutionProof,
                    )
                    if (
                        proof.trial_id != str(identity)
                        or proof.sample_id != intent.sample_id
                        or proof.protocol_ref != intent.protocol_ref
                        or proof.projection_ref != intent.projection_ref
                        or proof.job_id != receipt.job_id
                        or proof.current_run_id != receipt.current_run_id
                        or proof.initial_run_id != receipt.initial_run_id
                        or sum(c.origin_call_id is None for c in proof.model_calls)
                        != receipt.fresh_model_calls
                    ):
                        raise AgentGitError(
                            ErrorCode.OBJECT_CORRUPT, "验证原始调用来源与物理收据不同"
                        )
                    item = self.ledger.owned(
                        connection,
                        repo,
                        saved["evidence_ref"],
                        "repair_validation_trial",
                        identity,
                        "evidence",
                        ValidationTrialEvidence,
                    )
                    if item != self.evidence(receipt, saved["result_ref"]):
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "统计证据与实际验证收据不同")
                    evidence.append(item)
            waiting = TABLES["repair_validation_waits"]
            marker = (
                connection.execute(sa.select(waiting).where(waiting.c.id == identity))
                .mappings()
                .first()
            )
            if marker is not None:
                detail["waiting"] = dict(reason=marker["reason"], payload_ref=marker["payload_ref"])
            details.append(detail)
        return details, tuple(evidence)

    @staticmethod
    def evidence(receipt: RepairValidationReceipt, reference: str) -> ValidationTrialEvidence:
        return ValidationTrialEvidence.model_validate(
            dict(
                phase=receipt.phase,
                protocol_ref=receipt.protocol_ref,
                trial_id=receipt.trial_id,
                run_id=receipt.current_run_id,
                result_ref=reference,
                candidate_ref=receipt.candidate_ref,
                sample_ref=receipt.sample_ref,
                validator_ref=receipt.validator_ref,
                verdict=receipt.verdict,
                category=receipt.category,
                safety_violations=receipt.safety_violations,
                usage=receipt.usage,
            )
        )

    def claim(self, worker: str, *, repository: UUID | None = None) -> RepairStudyLease | None:
        if not worker or len(worker) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "确认控制器身份必须有界非空")
        table, openings = TABLES["repair_study_jobs"], TABLES["repair_validation_openings"]
        with self.history.engine.begin() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            query = sa.select(table).where(
                table.c.status.in_(
                    (
                        "validation_ready",
                        "confirming",
                        "regressing",
                        "waiting_reconciliation",
                        "cancelling",
                    )
                ),
                sa.or_(table.c.lease_until.is_(None), table.c.lease_until <= now),
                sa.exists().where(openings.c.id == table.c.study_id),
            )
            if repository is not None:
                query = query.where(table.c.repository_id == repository)
            job = (
                connection.execute(
                    query.order_by(table.c.created_at, table.c.study_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            source = self._source(connection, job["repository_id"], job["study_id"])
            if source.code_revision != source_revision() and not job["cancel_requested"]:
                self.ledger._update(
                    connection,
                    job,
                    "validation_incompatible",
                    {},
                    status="needs_attention",
                    stop_reason="code_revision_changed",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
                return None
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["repair_validation_trials"])
                .where(TABLES["repair_validation_trials"].c.study_id == job["study_id"])
            ).scalar_one()
            trials, results = (
                TABLES["repair_validation_trials"],
                TABLES["repair_validation_results"],
            )
            pending_phase = connection.execute(
                sa.select(trials.c.phase)
                .select_from(trials.outerjoin(results, results.c.id == trials.c.id))
                .where(trials.c.study_id == job["study_id"], results.c.id.is_(None))
                .order_by(trials.c.ordinal)
                .limit(1)
            ).scalar_one_or_none()
            status = (
                "cancelling"
                if job["cancel_requested"]
                else (
                    "waiting_reconciliation"
                    if job["status"] == "waiting_reconciliation"
                    else "confirming"
                    if pending_phase == "confirm" or (pending_phase is None and count < 6)
                    else "regressing"
                )
            )
            token, until = uuid4(), now + timedelta(milliseconds=self.ledger.lease_ms)
            self.ledger._update(
                connection,
                job,
                "validation_claimed",
                {"worker": worker},
                status=status,
                lease_token=token,
                lease_worker=worker,
                lease_until=until,
            )
            return RepairStudyLease(
                repository_id=str(job["repository_id"]),
                study_id=str(job["study_id"]),
                token=str(token),
                worker=worker,
                expires_at=until,
            )

    def reserve(self, lease: RepairStudyLease) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            if job["cancel_requested"] or job["status"] not in ("confirming", "regressing"):
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消或待核对时不能扩展验证")
            opening_row, opening = self._opening(connection, repo, study)
            _, suite = self.pools._suite(connection, repo, UUID(opening.suite_id))
            details, _ = self._records(connection, repo, study)
            pending = next((item for item in details if "result" not in item), None)
            if pending is not None:
                return dict(admitted=True, reused=True, trial_id=pending["trial_id"])
            ordinal = len(details)
            if ordinal >= 10:
                return dict(admitted=False, finished=True)
            item = (*suite.confirm, *suite.regression)[ordinal]
            source = self._source(connection, repo, study)
        if source.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码继续旧确认")
        projection = prepare_projection(
            self.research, repo, source, opening.plan.lock, item, source.budget.trial
        )
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            details, _ = self._records(connection, repo, study)
            if len(details) != ordinal or job["cancel_requested"]:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "验证准备期间控制或意图改变")
            root = self.ledger._root(connection, repo, study)
            allowed = admission(
                source.budget, self.ledger._consumption(connection, root), item.sample.role
            )
            if not allowed.allowed:
                self.ledger._update(
                    connection,
                    job,
                    "validation_budget_denied",
                    allowed.model_dump(mode="json"),
                    stop_reason=allowed.reason,
                )
                return dict(admitted=False, decision=allowed.model_dump(mode="json"))
            if source.code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "准备期间代码改变，未预留验证")
            identity = uuid4()
            intent = RepairValidationIntent(
                study_id=str(study),
                sample_id=item.ticket.generation_id,
                ordinal=ordinal,
                phase="confirm" if ordinal < 6 else "regression",
                protocol_ref=opening_row["plan_ref"],
                lock_ref=validation_ref(opening.plan.lock),
                registered=item,
                projection_ref=validation_ref(projection),
                branch_name=f"validate-{study.hex}-{ordinal}",
                limits=source.budget.trial,
            )
            for role, value in (("intent", intent), ("projection", projection)):
                obj = self.history.objects.put_json(ObjectType.BLOB, value.model_dump(mode="json"))
                self.history._ref(
                    connection, repo, obj, "repair_validation_trial", str(identity), role
                )
            if projection.hybrid_ref is not None:
                obj = self.history.objects.put(
                    ObjectType.BLOB,
                    self.history.objects.get(projection.hybrid_ref, ObjectType.BLOB),
                )
                self.history._ref(
                    connection, repo, obj, "repair_validation_trial", str(identity), "hybrid"
                )
            connection.execute(
                TABLES["repair_validation_trials"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    study_id=study,
                    sample_id=UUID(item.ticket.generation_id),
                    ordinal=ordinal,
                    phase=intent.phase,
                    protocol_ref=intent.protocol_ref,
                    projection_ref=intent.projection_ref,
                    intent_ref=validation_ref(intent),
                    intent_key=validation_ref(intent),
                    branch_name=intent.branch_name,
                )
            )
            self.ledger._update(
                connection,
                job,
                "validation_reserved",
                dict(trial_id=str(identity), sample_id=intent.sample_id),
                stop_reason=None,
            )
        return dict(admitted=True, reused=False, trial_id=str(identity))

    def finish(
        self, lease: RepairStudyLease, *, reason: str = "validation_complete"
    ) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            root = self.ledger._root(connection, repo, study)
            if self.ledger._consumption(connection, root).active:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "未决验证排空前不能封存摘要")
            _, opening = self._opening(connection, repo, study)
            _, evidence = self._records(connection, repo, study)
            if self._source(connection, repo, study).code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码封存旧验证")
            summary = verify_repair(opening.plan, evidence)
            body = self.history.objects.put_json(ObjectType.BLOB, summary.model_dump(mode="json"))
            self.history._ref(
                connection, repo, body, "repair_study", str(study), "validation_summary"
            )
            event, _ = self.ledger._event(
                connection,
                repo,
                study,
                job["revision"] + 1,
                "validation_finished",
                dict(summary_ref=body.id, reason=reason),
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
                    status="cancelled" if job["cancel_requested"] else summary.status,
                    revision=job["revision"] + 1,
                    stop_reason=reason,
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            )
        return self.view(repo, study)

    def view(self, repo: UUID, study: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            root = self.ledger._root(connection, repo, study)
            job = self.ledger._job(connection, repo, study)
            row, opening = self._opening(connection, repo, study)
            details, evidence = self._records(connection, repo, study)
            usage = self.ledger._consumption(connection, root)
            summary_table = TABLES["repair_validation_summaries"]
            saved = (
                connection.execute(sa.select(summary_table).where(summary_table.c.id == study))
                .mappings()
                .first()
            )
            summary = None
            if saved is not None:
                value = self.ledger.owned(
                    connection,
                    repo,
                    saved["summary_ref"],
                    "repair_study",
                    study,
                    "validation_summary",
                    RepairVerificationSummary,
                )
                if value.plan_ref != row["plan_ref"] or value.result_refs != tuple(
                    item.result_ref for item in evidence
                ):
                    raise AgentGitError(
                        ErrorCode.OBJECT_CORRUPT, "封存统计缺少正确计划或全部实际结果"
                    )
                summary = {"summary_ref": saved["summary_ref"], **value.model_dump(mode="json")}
            compatible = self._source(connection, repo, study).code_revision == source_revision()
            if job["status"] in STUDY_TERMINAL:
                event = TABLES["repair_study_events"]
                ended = connection.execute(
                    sa.select(event.c.created_at).where(
                        event.c.study_id == study, event.c.revision == job["revision"]
                    )
                ).scalar_one()
                source = self.ledger.screening._root(connection, repo, root["search_id"])
                usage = evolve(
                    usage,
                    elapsed_wall_ms=max(
                        0, int((ended - source["created_at"]).total_seconds() * 1000)
                    ),
                )
            response = dict(
                study_id=str(study),
                status=job["status"],
                cancel_requested=job["cancel_requested"],
                revision=job["revision"],
                stop_reason=job["stop_reason"],
                plan_ref=row["plan_ref"],
                usage=usage.model_dump(mode="json"),
                trials=details,
                verification=summary,
                provisional_verification=verify_repair(opening.plan, evidence).model_dump(
                    mode="json"
                )
                if compatible and summary is None
                else None,
            )
        if len(encode(response)) > 8 * 1024 * 1024:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "完整验证报告超过查询上限")
        return response
