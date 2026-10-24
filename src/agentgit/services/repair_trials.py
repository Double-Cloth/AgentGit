"""将冻结试验意图与真实独立 Replay Job 原子绑定；不在派发中执行 Agent。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import TERMINAL_RUNS
from agentgit.core.models import parse_contract
from agentgit.core.repair import (
    RepairLease,
    RepairProposal,
    RepairTrialIntent,
    RepairTrialResult,
    RepairTrialUsage,
)
from agentgit.core.research import AgentConfig, AgentState
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import LiveOverrides, LiveReplay, RecordedReplay
from agentgit.repair.space import patch_ref, proposal_key
from agentgit.services.environment import source_revision
from agentgit.services.interventions import InterventionService
from agentgit.services.jobs import JobService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


class RepairTrialService:
    def __init__(self, research: ResearchService, *, fault: Callable[[str], None] | None = None):
        self.research, self.history = research, research.history
        self.ledger = RepairJobService(self.history)
        self.replays = ReplayService(research)
        self.interventions = InterventionService(self.history)
        self.fault = fault or (lambda _: None)

    def _trial(self, connection: sa.Connection, lease: RepairLease, identity: UUID) -> Any:
        table = TABLES["repair_trials"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == UUID(lease.repository_id),
                    table.c.search_id == UUID(lease.search_id),
                    table.c.id == identity,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "试验意图不属于当前搜索")
        self.ledger._intent(row)
        return row

    def _dispatched(self, connection: sa.Connection, lease: RepairLease, identity: UUID) -> Any:
        table = TABLES["repair_trial_jobs"]
        return (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == UUID(lease.repository_id),
                    table.c.id == identity,
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _response(row: Any, *, reused: bool) -> dict[str, Any]:
        return {
            "trial_id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "initial_run_id": str(row["initial_run_id"]),
            "reused": reused,
        }

    def dispatch(self, lease: RepairLease, identity: UUID) -> dict[str, Any]:
        repository = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            trial = self._trial(connection, lease, identity)
            existing = self._dispatched(connection, lease, identity)
            if existing is not None:
                return self._response(existing, reused=True)
            if job["cancel_requested"]:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消后不能派发未启动的试验")
            if job["status"] == "waiting_reconciliation":
                raise AgentGitError(ErrorCode.APPROVAL_REQUIRED, "未决试验核对前不能派发新候选")
            root = self.ledger._root(connection, repository, UUID(lease.search_id))
            manifest = self.ledger._manifest(connection, root)
            proposal = None
            if trial["proposal_id"] is not None:
                table = TABLES["repair_proposals"]
                proposal = dict(
                    connection.execute(
                        sa.select(table).where(
                            table.c.repository_id == repository,
                            table.c.search_id == UUID(lease.search_id),
                            table.c.id == trial["proposal_id"],
                        )
                    )
                    .mappings()
                    .one()
                )
        if manifest.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "派发代码与冻结搜索不兼容")
        intent = parse_contract(
            self.history.objects.get(trial["intent_ref"], ObjectType.BLOB), RepairTrialIntent
        )
        if intent.limits != manifest.budget.trial:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "试验预算与冻结搜索不同")
        binding = manifest.space.binding
        if intent.operation == "recorded_preflight":
            prepared = self.replays.prepare(
                repository,
                RecordedReplay(
                    source_run_id=binding.failed_run_id,
                    from_commit=binding.base_commit,
                    until_commit=manifest.source_head_commit,
                    branch_name=intent.branch_name,
                ),
                execution_wall_ms=intent.limits.max_wall_ms,
            )
        else:
            if proposal is None:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "筛选试验缺少冻结提案")
            definition = parse_contract(
                self.history.objects.get(proposal["definition_ref"], ObjectType.BLOB),
                RepairProposal,
            )
            ordered = tuple(manifest.space.patches[index] for index in definition.ordered_indices)
            if (
                definition.key != proposal["proposal_key"]
                or definition.key != intent.proposal_ref
                or proposal_key(binding, ordered) != definition.key
                or tuple(patch_ref(patch) for patch in ordered) != definition.patch_refs
                or tuple(manifest.intervention_ids[index] for index in definition.ordered_indices)
                != definition.intervention_ids
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "筛选提案不符合已冻结有序空间")
            view = self.replays.versions.checkout(repository, binding.base_commit)
            frozen = self.history.objects.get_json(
                view["environment"]["input_ref"], ObjectType.BLOB
            )
            if not isinstance(frozen, dict) or "config" not in frozen:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "筛选起点缺少冻结配置")
            applied = self.interventions.apply(
                repository,
                definition.intervention_ids,
                source_run_id=binding.failed_run_id,
                base_commit=binding.base_commit,
                validator_ref=binding.validator_ref,
                config=parse_contract(encode(frozen["config"]), AgentConfig),
                snapshot=parse_contract(encode(view["snapshot"]), StateSnapshot),
            )
            if applied.replacements and definition.hybrid_ref is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "观测替换不能降级成 Live 执行")
            prepared = self.replays.prepare(
                repository,
                LiveReplay(
                    source_run_id=binding.failed_run_id,
                    from_commit=binding.base_commit,
                    branch_name=intent.branch_name,
                    budget=intent.limits,
                    overrides=LiveOverrides(config=applied.config, plan=applied.plan),
                ),
                hybrid_ref=definition.hybrid_ref,
                repair_proposal_id=proposal["id"] if definition.hybrid_ref else None,
            )
        if prepared.code_revision != manifest.code_revision:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "试验准备不能更换冻结代码")
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            self._trial(connection, lease, identity)
            existing = self._dispatched(connection, lease, identity)
            if existing is not None:
                return self._response(existing, reused=True)
            if job["cancel_requested"]:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "准备期间取消，试验没有派发")
            if job["status"] == "waiting_reconciliation":
                raise AgentGitError(ErrorCode.APPROVAL_REQUIRED, "准备期间出现未决试验，停止新派发")
            submitted = self.replays.publish(
                prepared, actor=root["actor"], key=trial["intent_key"], connection=connection
            )
            self.fault("after_job_created")
            values = dict(
                id=identity,
                repository_id=repository,
                job_id=UUID(submitted["job_id"]),
                initial_run_id=UUID(submitted["run_id"]),
            )
            connection.execute(TABLES["repair_trial_jobs"].insert().values(**values))
            self.ledger._update(
                connection,
                job,
                "trial_dispatched",
                {
                    "trial_id": str(identity),
                    "job_id": submitted["job_id"],
                    "initial_run_id": submitted["run_id"],
                },
            )
        self.fault("after_dispatch_commit")
        return self._response(values, reused=False)

    def _waiting(
        self,
        lease: RepairLease,
        identity: UUID,
        reason: str,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            trial = self._trial(connection, lease, identity)
            results = TABLES["repair_trial_results"]
            settled = connection.execute(
                sa.select(results.c.result_ref).where(
                    results.c.repository_id == UUID(lease.repository_id),
                    results.c.id == identity,
                )
            ).scalar_one_or_none()
            if settled is not None:
                result = parse_contract(
                    self.history.objects.get(settled, ObjectType.BLOB), RepairTrialResult
                )
                return {
                    "settled": True,
                    "reused": True,
                    "result_ref": settled,
                    "result": result.model_dump(mode="json"),
                }
            table = TABLES["repair_trial_waits"]
            found = connection.execute(sa.select(table.c.id).where(table.c.id == identity)).first()
            if found is None:
                details: dict[str, Any] = {
                    "schema_version": 1,
                    "trial_id": str(identity),
                    "reason": reason,
                    "intent_ref": trial["intent_ref"],
                }
                if observation is not None:
                    report = self.history.objects.put_json(ObjectType.BLOB, observation)
                    self.history._ref(
                        connection,
                        UUID(lease.repository_id),
                        report,
                        "repair_trial",
                        str(identity),
                        "waiting_report",
                    )
                    details["observation_ref"] = report.id
                obj = self.history.objects.put_json(
                    ObjectType.BLOB,
                    details,
                )
                self.history._ref(
                    connection,
                    UUID(lease.repository_id),
                    obj,
                    "repair_trial",
                    str(identity),
                    "waiting",
                )
                connection.execute(
                    table.insert().values(
                        id=identity,
                        repository_id=UUID(lease.repository_id),
                        reason=reason,
                        payload_ref=obj.id,
                    )
                )
            status = "cancelling" if job["cancel_requested"] else "waiting_reconciliation"
            if found is None or job["status"] != status:
                self.ledger._update(
                    connection,
                    job,
                    "trial_waiting",
                    {
                        "trial_id": str(identity),
                        "reason": reason,
                    },
                    status=status,
                    stop_reason=reason,
                )
        return {"settled": False, "reason": reason, "trial_id": str(identity)}

    @staticmethod
    def _usage(report: dict[str, Any]) -> RepairTrialUsage | None:
        actual = report["actual"]
        if report["mode"] == "recorded":
            return RepairTrialUsage(
                model_calls=0,
                tool_calls=0,
                tokens=0,
                cost_micro_usd=0,
                unit="mock_utf8_bytes",
                cost_basis="recorded_no_external_calls",
            )
        meter = actual["model_usage"]
        mock, provider = meter["mock"], meter["provider"]
        if (
            actual["unknown_outcomes"]
            or meter["pending_calls"]
            or meter["unknown_calls"]
            or mock["total_bytes"] is None
            or provider["accounted_tokens"] is None
            or provider["estimated_cost_micro_usd"] is None
        ):
            return None
        unit = (
            "mixed_units"
            if mock["calls"] and provider["calls"]
            else ("provider_tokens" if provider["calls"] else "mock_utf8_bytes")
        )
        return RepairTrialUsage.model_validate(
            {
                "model_calls": actual["model_calls"],
                "tool_calls": actual["tool_calls"],
                "tokens": mock["total_bytes"] + provider["accounted_tokens"],
                "cost_micro_usd": provider["estimated_cost_micro_usd"],
                "unit": unit,
                "cost_basis": "pricebook_estimate" if provider["calls"] else "local_mock_free",
            }
        )

    def settle(self, lease: RepairLease, identity: UUID) -> dict[str, Any]:
        repository = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            self.ledger._leased(connection, lease)
            trial = self._trial(connection, lease, identity)
            results = TABLES["repair_trial_results"]
            existing = (
                connection.execute(sa.select(results).where(results.c.id == identity))
                .mappings()
                .first()
            )
            if existing is not None:
                result = parse_contract(
                    self.history.objects.get(existing["result_ref"], ObjectType.BLOB),
                    RepairTrialResult,
                )
                return {
                    "settled": True,
                    "reused": True,
                    "result_ref": existing["result_ref"],
                    "result": result.model_dump(mode="json"),
                }
            dispatched = self._dispatched(connection, lease, identity)
            if dispatched is None:
                return {"settled": False, "reason": "not_dispatched", "trial_id": str(identity)}
            root = self.ledger._root(connection, repository, UUID(lease.search_id))
            manifest = self.ledger._manifest(connection, root)
        queue = JobService(self.research)
        job = queue.get(repository, dispatched["job_id"])
        if job["status"] not in TERMINAL_RUNS:
            if job["status"] == "outcome_unknown":
                return self._waiting(lease, identity, "outcome_unknown")
            return {"settled": False, "reason": "job_pending", "trial_id": str(identity)}
        if manifest.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码重判冻结搜索结果")
        report = self.replays.report(repository, dispatched["job_id"])
        if report is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "搜索试验缺少 Replay 证明")
        usage = self._usage(report)
        if usage is None:
            return self._waiting(lease, identity, "physical_usage_unknown", report)
        intent = parse_contract(
            self.history.objects.get(trial["intent_ref"], ObjectType.BLOB), RepairTrialIntent
        )
        category, verdict, determinate = "execution", "failed", False
        assessment = None
        safety: tuple[str, ...] = ()
        uncertainties: list[str] = []
        if intent.operation == "recorded_preflight":
            category = "recovery"
            determinate = bool(report["verified"])
            verdict = "chain_valid" if determinate else "failed"
            if report["actual"]["unmeasured_attempts"]:
                uncertainties.append("recovery_attempt_wall_not_fully_measured")
        else:
            if report["initialized"]:
                assessment = ValidatorService(self.history).evaluate(
                    repository,
                    manifest.space.binding.validator_ref,
                    job["run_id"],
                    job["checkpoint_commit_id"],
                    persist=False,
                )
                state = parse_contract(
                    encode(self.history.restore(repository, job["checkpoint_commit_id"]).data),
                    AgentState,
                )
                if state.error_code in ("forbidden", "unauthorized"):
                    safety = (state.error_code,)
                if (
                    assessment.verdict in ("passed", "failed")
                    and state.evaluation is not None
                    and state.error_code in (None, ErrorCode.VALIDATION_FAILED.value)
                    and not safety
                ):
                    category, determinate = "oracle", True
                    verdict = "passed" if assessment.verdict == "passed" else "failed"
            meter = report["actual"]["model_usage"]
            if meter["provider"]["estimated_usage_calls"]:
                uncertainties.append("provider_usage_conservatively_estimated")
        if job["status"] == "budget_exhausted":
            category, determinate, verdict = "budget", False, "failed"
        elif job["status"] == "cancelled":
            category, determinate, verdict = "cancelled", False, "failed"
        elif safety:
            category, determinate, verdict = "safety", False, "failed"
        over = any(
            value > cap
            for value, cap in (
                (usage.model_calls, intent.limits.max_model_calls),
                (usage.tool_calls, intent.limits.max_tool_calls),
                (usage.tokens, intent.limits.max_tokens),
                (usage.cost_micro_usd, intent.limits.max_cost_micro_usd or 0),
            )
        )
        if over:
            category, determinate, verdict = "budget", False, "failed"
            uncertainties.append("observed_usage_exceeds_reservation")
        report_obj = self.history.objects.put_json(ObjectType.BLOB, report)
        result = RepairTrialResult.model_validate(
            {
                "trial_id": str(identity),
                "job_id": str(dispatched["job_id"]),
                "initial_run_id": str(dispatched["initial_run_id"]),
                "current_run_id": str(job["run_id"]),
                "operation": intent.operation,
                "verdict": verdict,
                "category": category,
                "determinate": determinate,
                "usage": usage,
                "replay_report_ref": report_obj.id,
                "assessment": assessment,
                "safety_violations": safety,
                "uncertainties": tuple(uncertainties),
            }
        )
        result_obj = self.history.objects.put_json(ObjectType.BLOB, result.model_dump(mode="json"))
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            existing = (
                connection.execute(sa.select(results).where(results.c.id == identity))
                .mappings()
                .first()
            )
            if existing is not None:
                saved = parse_contract(
                    self.history.objects.get(existing["result_ref"], ObjectType.BLOB),
                    RepairTrialResult,
                )
                return {
                    "settled": True,
                    "reused": True,
                    "result_ref": existing["result_ref"],
                    "result": saved.model_dump(mode="json"),
                }
            current = (
                connection.execute(
                    sa.select(TABLES["jobs"])
                    .where(
                        TABLES["jobs"].c.id == dispatched["job_id"],
                        TABLES["jobs"].c.repository_id == repository,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if any(
                current[key] != job[key]
                for key in (
                    "run_id",
                    "status",
                    "checkpoint_commit_id",
                    "attempt",
                    "fencing_token",
                )
            ):
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "试验终态在结算前发生变化，请重新核对"
                )
            self.history._ref(
                connection, repository, report_obj, "repair_trial", str(identity), "replay_report"
            )
            self.history._ref(
                connection, repository, result_obj, "repair_trial", str(identity), "result"
            )
            connection.execute(
                results.insert().values(
                    id=identity,
                    repository_id=repository,
                    verdict=result.verdict,
                    result_ref=result_obj.id,
                    model_calls=usage.model_calls,
                    tool_calls=usage.tool_calls,
                    tokens=usage.tokens,
                    cost_micro_usd=usage.cost_micro_usd,
                )
            )
            self.fault("before_result_commit")
            waiting = TABLES["repair_trial_waits"]
            unresolved = connection.execute(
                sa.select(sa.func.count())
                .select_from(
                    waiting.join(
                        TABLES["repair_trials"],
                        TABLES["repair_trials"].c.id == waiting.c.id,
                    )
                )
                .where(
                    TABLES["repair_trials"].c.search_id == UUID(lease.search_id),
                    ~sa.exists(sa.select(results.c.id).where(results.c.id == waiting.c.id)),
                )
            ).scalar_one()
            status = (
                "cancelling"
                if controller["cancel_requested"]
                else ("waiting_reconciliation" if unresolved else "running")
            )
            values: dict[str, Any] = {
                "status": status,
                "stop_reason": "awaiting_trial_reconciliation" if unresolved else None,
            }
            if (
                controller["cancel_requested"]
                and not self.ledger._consumption(connection, root).active
            ):
                values.update(
                    status="cancelled",
                    stop_reason="operator_cancelled",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            if over:
                values.update(
                    status="needs_attention",
                    stop_reason="usage_above_reservation",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            self.ledger._update(
                connection,
                controller,
                "trial_settled",
                {
                    "trial_id": str(identity),
                    "result_ref": result_obj.id,
                    "verdict": result.verdict,
                    "determinate": determinate,
                },
                **values,
            )
        self.fault("after_result_commit")
        return {
            "settled": True,
            "reused": False,
            "result_ref": result_obj.id,
            "result": result.model_dump(mode="json"),
        }

    def request_cancellation(self, lease: RepairLease) -> int:
        """只传递已持久请求的取消；不恢复、重发或自动核对外部效果。"""
        repository = UUID(lease.repository_id)
        with self.history.engine.begin() as connection:
            controller = self.ledger._leased(connection, lease)
            if not controller["cancel_requested"]:
                raise AgentGitError(ErrorCode.FORBIDDEN, "必须先持久请求搜索取消")
            trials, dispatches, jobs, runs = (
                TABLES[name]
                for name in (
                    "repair_trials",
                    "repair_trial_jobs",
                    "jobs",
                    "runs",
                )
            )
            children = (
                connection.execute(
                    sa.select(runs)
                    .select_from(
                        trials.join(dispatches, dispatches.c.id == trials.c.id)
                        .join(jobs, jobs.c.id == dispatches.c.job_id)
                        .join(runs, runs.c.id == jobs.c.run_id)
                    )
                    .where(
                        trials.c.repository_id == repository,
                        trials.c.search_id == UUID(lease.search_id),
                    )
                )
                .mappings()
                .all()
            )
            count = 0
            for run in children:
                if run["status"] in TERMINAL_RUNS or run["control_status"] == "requested":
                    continue
                JobService(self.research).request_control(
                    repository,
                    run["id"],
                    "cancel",
                    expected_version=run["state_version"],
                    connection=connection,
                )
                count += 1
            if count:
                self.ledger._update(
                    connection, controller, "trial_cancellation_requested", {"count": count}
                )
            return count
