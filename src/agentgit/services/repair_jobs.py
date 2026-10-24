"""搜索编排的短事务租约和预算意图；本模块不调用 Agent 或外部工具。"""

from datetime import timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair import (
    PhaseQuotas,
    RepairConsumption,
    RepairLease,
    RepairSearchDecision,
    RepairSearchManifest,
    RepairTrialIntent,
    RepairTrialResult,
)
from agentgit.repair.budget import admission
from agentgit.repair.space import trial_key
from agentgit.services.environment import source_revision
from agentgit.services.history import HistoryService
from agentgit.storage.database import transaction
from agentgit.storage.schema import build_metadata
from agentgit.storage.schema_s9 import TERMINAL_SEARCHES

TABLES = build_metadata().tables


def record_repair_event(
    history: HistoryService,
    connection: sa.Connection,
    repository: UUID,
    search: UUID,
    revision: int,
    action: str,
    data: dict[str, Any],
) -> None:
    identity = uuid4()
    obj = history.objects.put_json(ObjectType.BLOB, {"schema_version": 1, **data})
    history._ref(connection, repository, obj, "repair_event", str(identity), "payload")
    connection.execute(
        TABLES["repair_events"]
        .insert()
        .values(
            id=identity,
            repository_id=repository,
            search_id=search,
            revision=revision,
            action=action,
            payload_ref=obj.id,
        )
    )


class RepairJobService:
    def __init__(self, history: HistoryService, *, lease_ms: int = 15_000):
        if type(lease_ms) is not int or not 100 <= lease_ms <= 120_000:
            raise AgentGitError(ErrorCode.VALIDATION, "搜索租约必须为 100–120000 毫秒")
        self.history, self.lease_ms = history, lease_ms

    def _root(self, connection: sa.Connection, repository: UUID, search: UUID) -> Any:
        table = TABLES["repair_searches"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.repository_id == repository, table.c.id == search)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "修复搜索不可访问")
        return row

    def _manifest(self, connection: sa.Connection, row: Any) -> RepairSearchManifest:
        refs = TABLES["object_refs"]
        if (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == row["repository_id"],
                    refs.c.owner_type == "repair_search",
                    refs.c.owner_id == str(row["id"]),
                    refs.c.role == "manifest",
                    refs.c.object_id == row["manifest_ref"],
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "搜索缺少自有冻结内容")
        manifest = parse_contract(
            self.history.objects.get(row["manifest_ref"], ObjectType.BLOB), RepairSearchManifest
        )
        binding = manifest.space.binding
        if (
            binding.repository_id != str(row["repository_id"])
            or binding.failed_run_id != str(row["failed_run_id"])
            or binding.base_commit != row["base_commit_id"]
            or binding.validator_ref != row["validator_ref"]
            or manifest.source_head_commit != row["source_head_commit_id"]
            or ("hybrid" if manifest.hybrid else "live") != row["screen_mode"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "搜索索引与冻结内容不同")
        return manifest

    def _job(self, connection: sa.Connection, repository: UUID, search: UUID) -> Any:
        table = TABLES["repair_jobs"]
        row = (
            connection.execute(
                sa.select(table)
                .where(
                    table.c.repository_id == repository,
                    table.c.search_id == search,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "搜索任务不可访问")
        return row

    def _update(
        self,
        connection: sa.Connection,
        job: Any,
        action: str,
        data: dict[str, Any],
        **values: Any,
    ) -> None:
        revision = job["revision"] + 1
        connection.execute(
            TABLES["repair_jobs"]
            .update()
            .where(
                TABLES["repair_jobs"].c.search_id == job["search_id"],
            )
            .values(revision=revision, **values)
        )
        record_repair_event(
            self.history, connection, job["repository_id"], job["search_id"], revision, action, data
        )

    def claim(self, worker: str, *, repository: UUID | None = None) -> RepairLease | None:
        if not worker or len(worker) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "搜索 Worker 名称必须是有界非空字符串")
        jobs = TABLES["repair_jobs"]
        with self.history.engine.begin() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            query = sa.select(jobs).where(
                jobs.c.status.in_(("queued", "running", "cancelling", "waiting_reconciliation")),
                sa.or_(jobs.c.lease_until.is_(None), jobs.c.lease_until <= now),
            )
            if repository is not None:
                query = query.where(jobs.c.repository_id == repository)
            job = (
                connection.execute(
                    query.order_by(jobs.c.created_at, jobs.c.search_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            root = self._root(connection, job["repository_id"], job["search_id"])
            manifest = self._manifest(connection, root)
            if manifest.code_revision != source_revision() and not job["cancel_requested"]:
                self._update(
                    connection,
                    job,
                    "incompatible_code",
                    {},
                    status="needs_attention",
                    stop_reason="incompatible_code",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
                return None
            token = uuid4()
            until = now + timedelta(milliseconds=self.lease_ms)
            self._update(
                connection,
                job,
                "claimed",
                {"worker": worker, "token": str(token)},
                status="cancelling"
                if job["cancel_requested"]
                else (
                    "waiting_reconciliation"
                    if job["status"] == "waiting_reconciliation"
                    else "running"
                ),
                lease_token=token,
                lease_worker=worker,
                lease_until=until,
            )
            return RepairLease(
                repository_id=str(job["repository_id"]),
                search_id=str(job["search_id"]),
                token=str(token),
                worker=worker,
                expires_at=until,
            )

    def _leased(self, connection: sa.Connection, lease: RepairLease) -> Any:
        job = self._job(connection, UUID(lease.repository_id), UUID(lease.search_id))
        now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        if (
            job["status"] in TERMINAL_SEARCHES
            or job["lease_token"] != UUID(lease.token)
            or job["lease_worker"] != lease.worker
            or job["lease_until"] is None
            or job["lease_until"] <= now
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "搜索租约已过期或被其他 Worker 替代")
        return job

    def heartbeat(self, lease: RepairLease) -> RepairLease:
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            until = now + timedelta(milliseconds=self.lease_ms)
            self._update(connection, job, "heartbeat", {}, lease_until=until)
        return RepairLease(
            repository_id=lease.repository_id,
            search_id=lease.search_id,
            token=lease.token,
            worker=lease.worker,
            expires_at=until,
        )

    def release(self, lease: RepairLease) -> None:
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            self._update(
                connection,
                job,
                "released",
                {},
                lease_token=None,
                lease_worker=None,
                lease_until=None,
            )

    def _consumption(self, connection: sa.Connection, root: Any) -> RepairConsumption:
        trials, results = TABLES["repair_trials"], TABLES["repair_trial_results"]
        rows = (
            connection.execute(
                sa.select(
                    trials.c.phase,
                    results.c.id.label("settled"),
                    results.c.model_calls,
                    results.c.tool_calls,
                    results.c.tokens,
                    results.c.cost_micro_usd,
                )
                .select_from(
                    trials.outerjoin(
                        results,
                        sa.and_(
                            results.c.id == trials.c.id,
                            results.c.repository_id == trials.c.repository_id,
                        ),
                    )
                )
                .where(
                    trials.c.search_id == root["id"],
                    trials.c.repository_id == root["repository_id"],
                )
            )
            .mappings()
            .all()
        )
        phases = {phase: 0 for phase in ("screen", "reduce", "confirm", "regression")}
        active, model, tool, tokens, cost = 0, 0, 0, 0, 0
        for row in rows:
            phases[row["phase"]] += 1
            if row["settled"] is None:
                active += 1
            else:
                model += row["model_calls"]
                tool += row["tool_calls"]
                tokens += row["tokens"]
                cost += row["cost_micro_usd"]
        now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        elapsed = max(0, int((now - root["created_at"]).total_seconds() * 1000))
        return RepairConsumption(
            started=PhaseQuotas.model_validate(phases),
            active=active,
            model_calls=model,
            tool_calls=tool,
            tokens=tokens,
            cost_micro_usd=cost,
            elapsed_wall_ms=elapsed,
        )

    def _intent(self, row: Any) -> dict[str, Any]:
        intent = parse_contract(
            self.history.objects.get(row["intent_ref"], ObjectType.BLOB), RepairTrialIntent
        )
        if (
            intent.search_id != str(row["search_id"])
            or intent.ordinal != row["ordinal"]
            or intent.phase != row["phase"]
            or intent.operation != row["operation"]
            or intent.branch_name != row["branch_name"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "试验意图索引与冻结内容不同")
        return {
            "trial_id": str(row["id"]),
            "intent_key": row["intent_key"],
            "intent_ref": row["intent_ref"],
            "intent": intent.model_dump(mode="json"),
        }

    def reserve(
        self,
        lease: RepairLease,
        proposal_id: UUID | None = None,
        *,
        finalize_denial: bool = True,
    ) -> dict[str, Any]:
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            if job["cancel_requested"]:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "搜索取消后不能新增试验意图")
            if job["status"] == "waiting_reconciliation":
                raise AgentGitError(ErrorCode.APPROVAL_REQUIRED, "必须先核对未决试验，不能扩展候选")
            root = self._root(connection, UUID(lease.repository_id), UUID(lease.search_id))
            manifest = self._manifest(connection, root)
            if manifest.code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "当前代码不兼容冻结搜索")
            operation: Literal["recorded_preflight", "screen"] = (
                "recorded_preflight" if proposal_id is None else "screen"
            )
            proposal_ref = root["manifest_ref"]
            if proposal_id is not None:
                proposals = TABLES["repair_proposals"]
                proposal_ref = connection.execute(
                    sa.select(proposals.c.proposal_key).where(
                        proposals.c.repository_id == UUID(lease.repository_id),
                        proposals.c.search_id == UUID(lease.search_id),
                        proposals.c.id == proposal_id,
                    )
                ).scalar_one_or_none()
                if proposal_ref is None:
                    raise AgentGitError(ErrorCode.NOT_FOUND, "提案不属于当前搜索")
            identity_key = trial_key(lease.search_id, proposal_ref, "screen", 0)
            trials = TABLES["repair_trials"]
            existing = (
                connection.execute(
                    sa.select(trials).where(
                        trials.c.search_id == UUID(lease.search_id),
                        trials.c.intent_key == identity_key,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return {"admitted": True, "reused": True, **self._intent(existing)}
            if proposal_id is not None:
                valid = connection.execute(
                    sa.select(trials.c.id)
                    .join(
                        TABLES["repair_trial_results"],
                        TABLES["repair_trial_results"].c.id == trials.c.id,
                    )
                    .where(
                        trials.c.search_id == UUID(lease.search_id),
                        trials.c.operation == "recorded_preflight",
                        TABLES["repair_trial_results"].c.verdict == "chain_valid",
                    )
                ).first()
                if valid is None:
                    raise AgentGitError(ErrorCode.VALIDATION, "筛选前必须完成 Recorded 恢复链验证")
            usage = self._consumption(connection, root)
            decision = admission(manifest.budget, usage, "screen")
            if not decision.allowed:
                values: dict[str, Any] = {"stop_reason": decision.reason}
                if finalize_denial and decision.reason != "concurrency" and usage.active == 0:
                    values.update(
                        status="budget_exhausted",
                        lease_token=None,
                        lease_worker=None,
                        lease_until=None,
                    )
                self._update(
                    connection, job, "budget_denied", decision.model_dump(mode="json"), **values
                )
                return {
                    "admitted": False,
                    "reused": False,
                    "decision": decision.model_dump(mode="json"),
                }
            ordinal = usage.started.total()
            identity = uuid4()
            intent = RepairTrialIntent(
                search_id=lease.search_id,
                proposal_ref=proposal_ref,
                phase="screen",
                repeat=0,
                ordinal=ordinal,
                operation=operation,
                branch_name=f"repair-{UUID(lease.search_id).hex}-{ordinal}",
                limits=manifest.budget.trial,
            )
            obj = self.history.objects.put_json(ObjectType.BLOB, intent.model_dump(mode="json"))
            self.history._ref(
                connection, UUID(lease.repository_id), obj, "repair_trial", str(identity), "intent"
            )
            values = dict(
                id=identity,
                repository_id=UUID(lease.repository_id),
                search_id=UUID(lease.search_id),
                proposal_id=proposal_id,
                ordinal=ordinal,
                phase="screen",
                operation=operation,
                branch_name=intent.branch_name,
                intent_key=identity_key,
                intent_ref=obj.id,
            )
            connection.execute(trials.insert().values(**values))
            self._update(
                connection,
                job,
                "trial_reserved",
                {
                    "trial_id": str(identity),
                    "intent_key": identity_key,
                    "limits": intent.limits.model_dump(mode="json"),
                },
                stop_reason=None,
            )
            return {"admitted": True, "reused": False, **self._intent(values)}

    def cancel(
        self,
        repository: UUID,
        search: UUID,
        *,
        actor: str,
        connection: sa.Connection | None = None,
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "取消必须记录有效操作者")
        with transaction(self.history.engine, connection) as connection:
            root = self._root(connection, repository, search)
            job = self._job(connection, repository, search)
            if (job["status"] in TERMINAL_SEARCHES and job["status"] != "needs_attention") or job[
                "cancel_requested"
            ]:
                return {
                    "search_id": str(search),
                    "status": job["status"],
                    "cancel_requested": job["cancel_requested"],
                }
            usage = self._consumption(connection, root)
            status = "cancelling" if usage.active else "cancelled"
            values: dict[str, Any] = {
                "cancel_requested": True,
                "status": status,
                "stop_reason": "operator_cancelled",
            }
            if not usage.active:
                values.update(lease_token=None, lease_worker=None, lease_until=None)
            self._update(
                connection,
                job,
                "cancel_requested",
                {"active": usage.active, "actor": actor},
                **values,
            )
            return {"search_id": str(search), "status": status, "cancel_requested": True}

    def abandon_undispatched(self, lease: RepairLease) -> int:
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            if not job["cancel_requested"]:
                raise AgentGitError(ErrorCode.FORBIDDEN, "只允许释放取消后确定未派发的意图")
            root = self._root(connection, UUID(lease.repository_id), UUID(lease.search_id))
            trials, dispatches, results = (
                TABLES[name]
                for name in ("repair_trials", "repair_trial_jobs", "repair_trial_results")
            )
            rows = (
                connection.execute(
                    sa.select(trials).where(
                        trials.c.search_id == UUID(lease.search_id),
                        ~sa.exists(
                            sa.select(dispatches.c.id).where(dispatches.c.id == trials.c.id)
                        ),
                        ~sa.exists(sa.select(results.c.id).where(results.c.id == trials.c.id)),
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                obj = self.history.objects.put_json(
                    ObjectType.BLOB,
                    {
                        "schema_version": 1,
                        "trial_id": str(row["id"]),
                        "verdict": "not_started",
                        "reason": "cancelled_before_dispatch",
                        "model_calls": 0,
                        "tool_calls": 0,
                        "tokens": 0,
                        "cost_micro_usd": 0,
                    },
                )
                self.history._ref(
                    connection,
                    UUID(lease.repository_id),
                    obj,
                    "repair_trial",
                    str(row["id"]),
                    "result",
                )
                connection.execute(
                    results.insert().values(
                        id=row["id"],
                        repository_id=UUID(lease.repository_id),
                        verdict="not_started",
                        result_ref=obj.id,
                        model_calls=0,
                        tool_calls=0,
                        tokens=0,
                        cost_micro_usd=0,
                    )
                )
            usage = self._consumption(connection, root)
            values: dict[str, Any] = {}
            if not usage.active:
                values.update(
                    status="cancelled", lease_token=None, lease_worker=None, lease_until=None
                )
            self._update(connection, job, "undispatched_cancelled", {"count": len(rows)}, **values)
            return len(rows)

    def view(self, repository: UUID, search: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            root = self._root(connection, repository, search)
            manifest = self._manifest(connection, root)
            jobs, trials, proposals = (
                TABLES[name] for name in ("repair_jobs", "repair_trials", "repair_proposals")
            )
            job = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.search_id == search, jobs.c.repository_id == repository
                    )
                )
                .mappings()
                .one()
            )
            usage = self._consumption(connection, root)
            trial_rows = (
                connection.execute(
                    sa.select(trials).where(trials.c.search_id == search).order_by(trials.c.ordinal)
                )
                .mappings()
                .all()
            )
            proposal_rows = (
                connection.execute(
                    sa.select(proposals)
                    .where(proposals.c.search_id == search)
                    .order_by(proposals.c.ordinal)
                )
                .mappings()
                .all()
            )
            details = [self._intent(row) for row in trial_rows]
            trial_ids = [row["id"] for row in trial_rows]
            by_id = {UUID(detail["trial_id"]): detail for detail in details}
            for table_name, key in (
                ("repair_trial_jobs", "dispatch"),
                ("repair_trial_results", "result"),
                ("repair_trial_waits", "waiting"),
            ):
                table = TABLES[table_name]
                rows = (
                    connection.execute(
                        sa.select(table).where(
                            table.c.repository_id == repository,
                            table.c.id.in_(trial_ids),
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    value: dict[str, Any]
                    if key == "dispatch":
                        value = {
                            "job_id": str(row["job_id"]),
                            "initial_run_id": str(row["initial_run_id"]),
                        }
                    elif key == "waiting":
                        value = {"reason": row["reason"], "payload_ref": row["payload_ref"]}
                    else:
                        payload = self.history.objects.get(row["result_ref"], ObjectType.BLOB)
                        if row["verdict"] == "not_started":
                            value = {
                                "verdict": "not_started",
                                "result_ref": row["result_ref"],
                                "category": "cancelled",
                                "determinate": False,
                            }
                        else:
                            result = parse_contract(payload, RepairTrialResult)
                            if (
                                result.trial_id != str(row["id"])
                                or result.verdict != row["verdict"]
                                or any(
                                    getattr(result.usage, name) != row[name]
                                    for name in (
                                        "model_calls",
                                        "tool_calls",
                                        "tokens",
                                        "cost_micro_usd",
                                    )
                                )
                            ):
                                raise AgentGitError(
                                    ErrorCode.OBJECT_CORRUPT, "试验结果与结算索引不同"
                                )
                            value = {
                                **result.model_dump(mode="json"),
                                "result_ref": row["result_ref"],
                            }
                    by_id[row["id"]][key] = value
            response = {
                "search_id": str(search),
                "repository_id": str(repository),
                "manifest_ref": root["manifest_ref"],
                "manifest": manifest.model_dump(mode="json"),
                "status": job["status"],
                "cancel_requested": job["cancel_requested"],
                "revision": job["revision"],
                "stop_reason": job["stop_reason"],
                "usage": usage.model_dump(mode="json"),
                "trials": details,
                "proposals": [
                    {
                        "proposal_id": str(row["id"]),
                        "ordinal": row["ordinal"],
                        "proposal_key": row["proposal_key"],
                        "definition_ref": row["definition_ref"],
                    }
                    for row in proposal_rows
                ],
            }
            events = TABLES["repair_events"]
            stopped = connection.execute(
                sa.select(events.c.payload_ref)
                .where(
                    events.c.repository_id == repository,
                    events.c.search_id == search,
                    events.c.action == "search_stopped",
                )
                .order_by(events.c.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            if stopped is not None:
                decision = parse_contract(
                    self.history.objects.get(stopped, ObjectType.BLOB), RepairSearchDecision
                )
                if (
                    decision.search_id != str(search)
                    or decision.manifest_ref != root["manifest_ref"]
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "停止报告与冻结搜索不符")
                response["decision"] = {**decision.model_dump(mode="json"), "decision_ref": stopped}
            if job["status"] in TERMINAL_SEARCHES:
                if stopped is not None and decision.status == job["status"]:
                    elapsed = decision.usage.elapsed_wall_ms
                else:
                    ended = connection.execute(
                        sa.select(events.c.created_at).where(
                            events.c.repository_id == repository,
                            events.c.search_id == search,
                            events.c.revision == job["revision"],
                        )
                    ).scalar_one_or_none()
                    if ended is None:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "终态缺少对应审计时刻")
                    elapsed = max(0, int((ended - root["created_at"]).total_seconds() * 1000))
                response["usage"]["elapsed_wall_ms"] = elapsed
            if len(encode(response)) > 8 * 1024 * 1024:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "搜索查询超过有界大小，不能截断证据")
            return response

    def listing(
        self,
        repository: UUID,
        *,
        after_id: UUID | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentGitError(ErrorCode.VALIDATION, "搜索列表上限必须为一至一百")
        roots, jobs, repos = (
            TABLES[name] for name in ("repair_searches", "repair_jobs", "repositories")
        )
        with self.history.engine.connect() as connection:
            if (
                connection.execute(sa.select(repos.c.id).where(repos.c.id == repository)).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
            query = (
                sa.select(
                    roots.c.id,
                    roots.c.failed_run_id,
                    roots.c.manifest_ref,
                    roots.c.proposal_count,
                    jobs.c.status,
                    jobs.c.stop_reason,
                )
                .join(
                    jobs,
                    sa.and_(jobs.c.search_id == roots.c.id, jobs.c.repository_id == repository),
                )
                .where(roots.c.repository_id == repository)
            )
            if after_id is not None:
                query = query.where(roots.c.id > after_id)
            rows = connection.execute(query.order_by(roots.c.id).limit(limit + 1)).mappings().all()
            return {
                "items": [
                    {
                        "search_id": str(row["id"]),
                        "failed_run_id": str(row["failed_run_id"]),
                        "manifest_ref": row["manifest_ref"],
                        "proposal_count": row["proposal_count"],
                        "status": row["status"],
                        "stop_reason": row["stop_reason"],
                    }
                    for row in rows[:limit]
                ],
                "next_after_id": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }
