"""S10 独立编排与共享原搜索预算；不运行模型、工具或读取确认响应。"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, parse_contract
from agentgit.core.repair import (
    BudgetDecision,
    RepairConsumption,
    RepairProposal,
    RepairSearchManifest,
    RepairTrialResult,
)
from agentgit.core.repair_studies import (
    NewRepairStudy,
    RepairReductionIntent,
    RepairReductionReceipt,
    RepairStudyLease,
    RepairStudyManifest,
)
from agentgit.core.repair_validation import (
    FinalRepairLock,
    ReductionState,
    ReductionSummary,
    RepairAtom,
    RepairCandidate,
    ValidationSample,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import AgentConfig, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.experiments.validation import frozen_inputs
from agentgit.repair.budget import admission
from agentgit.repair.policy import repair_model_scope
from agentgit.repair.reduction import lock_reduced_candidate, next_deletion, summarize_reduction
from agentgit.repair.space import patch_ref
from agentgit.services.environment import source_revision
from agentgit.services.history import HistoryService
from agentgit.services.hybrid import freeze_policy
from agentgit.services.interventions import InterventionService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.database import transaction
from agentgit.storage.schema import build_metadata
from agentgit.storage.schema_s10_confirmation import STUDY_TERMINAL

TABLES = build_metadata().tables


class RepairStudyService:
    def __init__(self, history: HistoryService, *, lease_ms: int = 120_000):
        if type(lease_ms) is not int or not 100 <= lease_ms <= 120_000:
            raise AgentGitError(ErrorCode.VALIDATION, "缩减租约必须为 100–120000 毫秒")
        self.history, self.lease_ms = history, lease_ms
        self.screening = RepairJobService(history)

    def owned[T: Contract](
        self,
        connection: sa.Connection,
        repository: UUID,
        reference: str,
        owner: str,
        identity: UUID,
        role: str,
        model: type[T],
    ) -> T:
        refs = TABLES["object_refs"]
        if (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository,
                    refs.c.object_id == reference,
                    refs.c.owner_type == owner,
                    refs.c.owner_id == str(identity),
                    refs.c.role == role,
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "缩减对象缺少自有冻结引用")
        return parse_contract(self.history.objects.get(reference, ObjectType.BLOB), model)

    def _root(self, connection: sa.Connection, repository: UUID, study: UUID) -> Any:
        table = TABLES["repair_studies"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.repository_id == repository, table.c.id == study)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "修复缩减不可访问")
        return row

    def _manifest(self, connection: sa.Connection, row: Any) -> RepairStudyManifest:
        manifest = self.owned(
            connection,
            row["repository_id"],
            row["manifest_ref"],
            "repair_study",
            row["id"],
            "manifest",
            RepairStudyManifest,
        )
        if (
            manifest.search_id != str(row["search_id"])
            or manifest.field_policy != row["field_policy"]
            or manifest.selected_proposal_id != str(row["selected_proposal_id"])
            or manifest.selected_trial_id != str(row["selected_trial_id"])
            or validation_ref(manifest.initial_state) != row["initial_state_ref"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "缩减根索引与冻结内容不同")
        return manifest

    def _job(self, connection: sa.Connection, repository: UUID, study: UUID) -> Any:
        table = TABLES["repair_study_jobs"]
        return (
            connection.execute(
                sa.select(table)
                .where(table.c.repository_id == repository, table.c.study_id == study)
                .with_for_update()
            )
            .mappings()
            .one()
        )

    def _state(self, connection: sa.Connection, job: Any) -> ReductionState:
        state = self.owned(
            connection,
            job["repository_id"],
            job["state_ref"],
            "repair_study",
            job["study_id"],
            "reduction_state",
            ReductionState,
        )
        summarize_reduction(state)
        manifest = self._manifest(
            connection, self._root(connection, job["repository_id"], job["study_id"])
        )
        if any(
            getattr(state, name) != getattr(manifest.initial_state, name)
            for name in (
                "initial",
                "sample",
                "screening_run_ids",
                "screening_result_refs",
                "max_trials",
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "删除状态不能改变原冻结候选或策略")
        return state

    def _event(
        self,
        connection: sa.Connection,
        repository: UUID,
        study: UUID,
        revision: int,
        action: str,
        data: dict[str, Any],
        *,
        identity: UUID | None = None,
    ) -> tuple[UUID, str]:
        identity = identity or uuid4()
        obj = self.history.objects.put_json(ObjectType.BLOB, {"schema_version": 1, **data})
        self.history._ref(
            connection, repository, obj, "repair_study_event", str(identity), "payload"
        )
        connection.execute(
            TABLES["repair_study_events"]
            .insert()
            .values(
                id=identity,
                repository_id=repository,
                study_id=study,
                revision=revision,
                action=action,
                payload_ref=obj.id,
            )
        )
        return identity, obj.id

    def _update(
        self, connection: sa.Connection, job: Any, action: str, data: dict[str, Any], **values: Any
    ) -> None:
        revision = job["revision"] + 1
        connection.execute(
            TABLES["repair_study_jobs"]
            .update()
            .where(TABLES["repair_study_jobs"].c.study_id == job["study_id"])
            .values(revision=revision, **values)
        )
        self._event(connection, job["repository_id"], job["study_id"], revision, action, data)

    @staticmethod
    def _response(row: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository_id": str(row["repository_id"]),
            "study_id": str(row["id"]),
            "search_id": str(row["search_id"]),
            "manifest_ref": row["manifest_ref"],
        }

    def _cached(
        self, connection: sa.Connection, actor: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        table = TABLES["repair_studies"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.actor == actor, table.c.request_key == key)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一缩减幂等键不能用于不同请求")
        return self._response(row)

    def submit(
        self, repository: UUID, request: NewRepairStudy, *, actor: str, key: str
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200 or not key or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "缩减操作者和幂等键必须有界非空")
        digest = object_digest(
            ObjectType.BLOB, encode([str(repository), request.model_dump(mode="json")])
        )
        with self.history.engine.connect() as connection:
            cached = self._cached(connection, actor, key, digest)
        if cached is not None:
            return cached
        search = UUID(request.search_id)
        view = self.screening.view(repository, search)
        if (
            view["status"] != "needs_confirmation"
            or view["cancel_requested"]
            or view["usage"]["active"]
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "缩减来源必须为已排空的待确认筛选")
        source_manifest = parse_contract(encode(view["manifest"]), RepairSearchManifest)
        if source_manifest.code_revision != source_revision():
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "旧代码搜索只读保留，不能用新代码继续缩减"
            )
        decision = view.get("decision")
        if decision is None or decision["status"] != "needs_confirmation":
            raise AgentGitError(ErrorCode.VALIDATION, "缺少冻结的待确认决定")
        selected = next(
            (
                t
                for t in view["trials"]
                if t.get("result", {}).get("result_ref") == decision["selected_result_ref"]
            ),
            None,
        )
        proposal = next(
            (p for p in view["proposals"] if p["proposal_id"] == decision["selected_proposal_id"]),
            None,
        )
        if selected is None or proposal is None:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "选择决定缺少独立提案/结果")
        outcome = parse_contract(
            encode({k: v for k, v in selected["result"].items() if k != "result_ref"}),
            RepairTrialResult,
        )
        if outcome.verdict != "passed" or not outcome.determinate or outcome.safety_violations:
            raise AgentGitError(ErrorCode.FORBIDDEN, "没有安全硬验收通过的候选不能进入缩减")
        definition = parse_contract(
            self.history.objects.get(proposal["definition_ref"], ObjectType.BLOB), RepairProposal
        )
        if (
            definition.key != decision["selected_proposal_key"]
            or selected["intent"]["proposal_ref"] != definition.key
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "选择提案身份不同")
        binding = source_manifest.space.binding
        base = VersionService(self.history).checkout(repository, binding.base_commit)
        frozen = self.history.objects.get_json(base["environment"]["input_ref"], ObjectType.BLOB)
        config = parse_contract(encode(frozen["config"]), AgentConfig)
        validator = ValidatorService(self.history).manifest(repository, binding.validator_ref)
        frozen_inputs(self.history.objects, validator)
        with self.history.engine.connect() as connection:
            calls = TABLES["external_calls"]
            origins = [
                dict(row)
                for row in connection.execute(
                    sa.select(calls.c.id, calls.c.intent_ref, calls.c.result_ref)
                    .where(
                        calls.c.repository_id == repository,
                        calls.c.run_id == UUID(binding.failed_run_id),
                    )
                    .order_by(calls.c.id)
                ).mappings()
            ]
            event = connection.execute(
                sa.select(TABLES["repair_events"].c.id).where(
                    TABLES["repair_events"].c.repository_id == repository,
                    TABLES["repair_events"].c.search_id == search,
                    TABLES["repair_events"].c.payload_ref == decision["decision_ref"],
                    TABLES["repair_events"].c.action == "search_stopped",
                )
            ).scalar_one()
        origin = self.history.objects.put_json(
            ObjectType.BLOB,
            {
                "schema_version": 1,
                "kind": "repair_response_origin/v1",
                "source_run_id": binding.failed_run_id,
                "base_commit": binding.base_commit,
                "head_commit": source_manifest.source_head_commit,
                "calls": [
                    {str(k): str(v) if isinstance(v, UUID) else v for k, v in row.items()}
                    for row in origins
                ],
            },
        )
        atoms = tuple(
            RepairAtom(
                patch_ref=definition.patch_refs[n],
                operator=source_manifest.space.patches[index].intervention.operator,
            )
            for n, index in enumerate(definition.ordered_indices)
        )
        candidate = RepairCandidate(
            binding_ref=view["manifest_ref"],
            source_proposal_ref=definition.key,
            task_ref=validator.input_ref,
            atoms=atoms,
            model_scope=repair_model_scope(config, source_manifest.fixture_scope),
        )
        maximum = (
            request.max_reduce_trials
            if request.max_reduce_trials is not None
            else source_manifest.budget.quotas.reduce
        )
        if maximum > source_manifest.budget.quotas.reduce:
            raise AgentGitError(ErrorCode.FORBIDDEN, "缩减次数不能扩大原搜索授权")
        state = ReductionState(
            initial=candidate,
            sample=ValidationSample(
                role="reduce",
                task_ref=validator.input_ref,
                response_set_ref=origin.id,
                validator_ref=binding.validator_ref,
            ),
            screening_run_ids=tuple(dict.fromkeys((binding.failed_run_id, outcome.current_run_id))),
            screening_result_refs=tuple(decision["result_refs"]),
            max_trials=maximum,
        )
        manifest = RepairStudyManifest(
            search_id=request.search_id,
            source_manifest_ref=view["manifest_ref"],
            source_decision_ref=decision["decision_ref"],
            selected_proposal_id=proposal["proposal_id"],
            selected_trial_id=selected["trial_id"],
            selected_result_ref=decision["selected_result_ref"],
            initial_state=state,
            code_revision=source_manifest.code_revision,
            field_policy=request.field_policy,
        )
        identity = uuid4()
        with self.history.engine.begin() as connection:
            connection.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(encode([actor, "repair-study", key]).decode(), 0)
                    )
                )
            )
            cached = self._cached(connection, actor, key, digest)
            if cached is not None:
                return cached
            self.screening._root(connection, repository, search)
            source_job = self.screening._job(connection, repository, search)
            table = TABLES["repair_studies"]
            if (
                connection.execute(
                    sa.select(table.c.id).where(
                        table.c.repository_id == repository, table.c.search_id == search
                    )
                ).first()
                is not None
            ):
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "同一筛选已经有唯一的后续缩减，不能重新选补丁"
                )
            if (
                source_job["status"] != "needs_confirmation"
                or source_job["revision"] != view["revision"]
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "筛选状态在缩减发布前发生变化")
            if source_revision() != manifest.code_revision:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "缩减准备和发布的代码不同")
            state_obj = self.history.objects.put_json(
                ObjectType.BLOB, state.model_dump(mode="json")
            )
            obj = self.history.objects.put_json(ObjectType.BLOB, manifest.model_dump(mode="json"))
            for role, body in (
                ("manifest", obj),
                ("reduction_state", state_obj),
                ("response_origin", origin),
            ):
                self.history._ref(connection, repository, body, "repair_study", str(identity), role)
            values = dict(
                id=identity,
                repository_id=repository,
                search_id=search,
                selected_proposal_id=UUID(proposal["proposal_id"]),
                selected_trial_id=UUID(selected["trial_id"]),
                source_event_id=event,
                manifest_ref=obj.id,
                initial_state_ref=state_obj.id,
                actor=actor,
                request_key=key,
                request_digest=digest,
                field_policy=request.field_policy,
            )
            connection.execute(table.insert().values(**values))
            connection.execute(
                TABLES["repair_study_jobs"]
                .insert()
                .values(study_id=identity, repository_id=repository, state_ref=state_obj.id)
            )
            self._event(
                connection,
                repository,
                identity,
                0,
                "created",
                {"source_search_id": str(search), "state_ref": state_obj.id},
            )
        return self._response(values)

    def claim(self, worker: str, *, repository: UUID | None = None) -> RepairStudyLease | None:
        if not worker or len(worker) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "缩减 Worker 名称必须有界非空")
        table = TABLES["repair_study_jobs"]
        with self.history.engine.begin() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            query = sa.select(table).where(
                table.c.status.in_(("queued", "reducing", "cancelling", "waiting_reconciliation")),
                sa.or_(table.c.lease_until.is_(None), table.c.lease_until <= now),
                ~sa.exists().where(TABLES["repair_validation_openings"].c.id == table.c.study_id),
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
            root = self._root(connection, job["repository_id"], job["study_id"])
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
            token, until = uuid4(), now + timedelta(milliseconds=self.lease_ms)
            status = (
                "cancelling"
                if job["cancel_requested"]
                else "waiting_reconciliation"
                if job["status"] == "waiting_reconciliation"
                else "reducing"
            )
            self._update(
                connection,
                job,
                "claimed",
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

    def _leased(self, connection: sa.Connection, lease: RepairStudyLease) -> Any:
        job = self._job(connection, UUID(lease.repository_id), UUID(lease.study_id))
        now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        if (
            job["status"] in (*STUDY_TERMINAL, "awaiting_samples")
            or job["lease_token"] != UUID(lease.token)
            or job["lease_worker"] != lease.worker
            or job["lease_until"] is None
            or job["lease_until"] <= now
        ):
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "缩减控制租约已失效")
        return job

    def release(self, lease: RepairStudyLease) -> None:
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

    def heartbeat(self, lease: RepairStudyLease) -> RepairStudyLease:
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            until = now + timedelta(milliseconds=self.lease_ms)
            self._update(connection, job, "heartbeat", {}, lease_until=until)
        return evolve(lease, expires_at=until)

    def _consumption(self, connection: sa.Connection, study: Any) -> RepairConsumption:
        root = self.screening._root(connection, study["repository_id"], study["search_id"])
        usage = self.screening._consumption(connection, root)
        trials, results = TABLES["repair_reduction_trials"], TABLES["repair_reduction_results"]
        rows = (
            connection.execute(
                sa.select(results)
                .select_from(trials.outerjoin(results, results.c.id == trials.c.id))
                .where(
                    trials.c.study_id == study["id"],
                    trials.c.repository_id == study["repository_id"],
                )
            )
            .mappings()
            .all()
        )
        usage = evolve(
            usage,
            started=evolve(usage.started, reduce=usage.started.reduce + len(rows)),
            active=usage.active + sum(row["id"] is None for row in rows),
            **{
                name: getattr(usage, name) + sum(row[name] for row in rows if row["id"] is not None)
                for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
            },
        )
        validation, settled = (
            TABLES["repair_validation_trials"],
            TABLES["repair_validation_results"],
        )
        outcomes = (
            connection.execute(
                sa.select(validation.c.phase, settled)
                .select_from(validation.outerjoin(settled, settled.c.id == validation.c.id))
                .where(
                    validation.c.study_id == study["id"],
                    validation.c.repository_id == study["repository_id"],
                )
            )
            .mappings()
            .all()
        )
        fields, field_results = TABLES["repair_field_trials"], TABLES["repair_field_results"]
        field_outcomes = (
            connection.execute(
                sa.select(field_results)
                .select_from(fields.outerjoin(field_results, field_results.c.id == fields.c.id))
                .where(
                    fields.c.study_id == study["id"],
                    fields.c.repository_id == study["repository_id"],
                )
            )
            .mappings()
            .all()
        )
        return evolve(
            usage,
            started=evolve(
                usage.started,
                reduce=usage.started.reduce + len(field_outcomes),
                confirm=usage.started.confirm + sum(row["phase"] == "confirm" for row in outcomes),
                regression=usage.started.regression
                + sum(row["phase"] == "regression" for row in outcomes),
            ),
            active=usage.active + sum(row["id"] is None for row in (*outcomes, *field_outcomes)),
            **{
                name: getattr(usage, name)
                + sum(row[name] for row in (*outcomes, *field_outcomes) if row["id"] is not None)
                for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
            },
        )

    def _applied(
        self,
        repository: UUID,
        source: Any,
        candidate: RepairCandidate,
        *,
        study_id: UUID | None = None,
    ) -> Any:
        if candidate.binding_ref != validation_ref(source):
            raise AgentGitError(ErrorCode.FORBIDDEN, "缩减候选不能更换源搜索绑定")
        patches = {
            patch_ref(p): (p, identity)
            for p, identity in zip(source.space.patches, source.intervention_ids, strict=True)
        }
        missing = tuple(atom.patch_ref for atom in candidate.atoms if atom.patch_ref not in patches)
        if missing:
            if study_id is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "派生候选必须指定所属 study")
            from agentgit.services.repair_fields import RepairFieldService

            with self.history.engine.connect() as connection:
                service = RepairFieldService(self.history)
                for reference in missing:
                    patches[reference] = service.resolve_patch(
                        connection, repository, study_id, source, reference
                    )
        if any(
            atom.patch_ref not in patches
            or atom.operator != patches[atom.patch_ref][0].intervention.operator
            for atom in candidate.atoms
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "缩减候选不能添加或修改原子内容")
        base = VersionService(self.history).checkout(repository, source.space.binding.base_commit)
        frozen = self.history.objects.get_json(base["environment"]["input_ref"], ObjectType.BLOB)
        return InterventionService(self.history).apply(
            repository,
            tuple(patches[atom.patch_ref][1] for atom in candidate.atoms),
            source_run_id=source.space.binding.failed_run_id,
            base_commit=source.space.binding.base_commit,
            validator_ref=source.space.binding.validator_ref,
            config=parse_contract(encode(frozen["config"]), AgentConfig),
            snapshot=parse_contract(encode(base["snapshot"]), StateSnapshot),
        )

    def reserve(self, lease: RepairStudyLease) -> dict[str, Any]:
        repository, study_id = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            if job["cancel_requested"] or job["status"] == "waiting_reconciliation":
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消或待核对时不能扩展缩减")
            study = self._root(connection, repository, study_id)
            manifest, state = self._manifest(connection, study), self._state(connection, job)
            root = self.screening._root(connection, repository, study["search_id"])
            source = self.screening._manifest(connection, root)
            if source_revision() != manifest.code_revision:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码预留旧缩减")
            protocol = job["state_ref"]
            existing = (
                connection.execute(
                    sa.select(TABLES["repair_reduction_trials"]).where(
                        TABLES["repair_reduction_trials"].c.study_id == study_id,
                        TABLES["repair_reduction_trials"].c.protocol_ref == protocol,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return {"admitted": True, "reused": True, "trial_id": str(existing["id"])}
            request = next_deletion(state)
            if request is None:
                return {"admitted": False, "finished": True}
        applied = self._applied(repository, source, request.after)
        hybrid = None
        if source.hybrid is not None:
            validator = ValidatorService(self.history).manifest(
                repository, source.space.binding.validator_ref
            )
            _, corpus = frozen_inputs(self.history.objects, validator)
            hybrid = freeze_policy(
                self.history,
                repository,
                source.space.binding.failed_run_id,
                source.space.binding.base_commit,
                source.space.binding.validator_ref,
                corpus,
                source.hybrid,
                applied.replacements,
            )
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            if (
                job["state_ref"] != protocol
                or job["cancel_requested"]
                or job["status"] != "reducing"
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "缩减状态在准备期间变化")
            trials = TABLES["repair_reduction_trials"]
            existing = connection.execute(
                sa.select(trials.c.id).where(
                    trials.c.study_id == study_id, trials.c.protocol_ref == protocol
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"admitted": True, "reused": True, "trial_id": str(existing)}
            usage = self._consumption(connection, study)
            allowed = admission(source.budget, usage, "reduce")
            if not allowed.allowed:
                self._update(
                    connection,
                    job,
                    "budget_denied",
                    allowed.model_dump(mode="json"),
                    stop_reason=allowed.reason,
                )
                return {"admitted": False, "decision": allowed.model_dump(mode="json")}
            ordinal = usage.started.reduce
            identity = uuid4()
            intent = RepairReductionIntent(
                study_id=str(study_id),
                ordinal=ordinal,
                protocol_ref=protocol,
                deletion=request,
                branch_name=f"reduce-{study_id.hex}-{ordinal}",
                limits=source.budget.trial,
                hybrid_ref=hybrid.id if hybrid else None,
            )
            obj = self.history.objects.put_json(ObjectType.BLOB, intent.model_dump(mode="json"))
            candidate = self.history.objects.put_json(
                ObjectType.BLOB, request.after.model_dump(mode="json")
            )
            for role, body in (("intent", obj), ("candidate", candidate)):
                self.history._ref(
                    connection, repository, body, "repair_reduction_trial", str(identity), role
                )
            if hybrid is not None:
                self.history._ref(
                    connection,
                    repository,
                    hybrid,
                    "repair_reduction_trial",
                    str(identity),
                    "hybrid",
                )
            connection.execute(
                trials.insert().values(
                    id=identity,
                    repository_id=repository,
                    study_id=study_id,
                    ordinal=ordinal,
                    phase="reduce",
                    protocol_ref=protocol,
                    candidate_ref=candidate.id,
                    intent_key=validation_ref(intent),
                    intent_ref=obj.id,
                    branch_name=intent.branch_name,
                )
            )
            self._update(
                connection,
                job,
                "trial_reserved",
                {"trial_id": str(identity), "protocol_ref": protocol},
                stop_reason=None,
            )
            return {"admitted": True, "reused": False, "trial_id": str(identity)}

    def seal(
        self, lease: RepairStudyLease, *, budget_stop: BudgetDecision | None = None
    ) -> dict[str, Any]:
        repository, identity = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self._leased(connection, lease)
            study = self._root(connection, repository, identity)
            if study["field_policy"] is not None:
                raise AgentGitError(
                    ErrorCode.CONTROL_REQUESTED, "启用字段策略的 study 必须先完成锁前字段阶段"
                )
            state = self._state(connection, job)
            if job["cancel_requested"] or self._consumption(connection, study).active:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消/未决项排空前不能锁定候选")
            source = self.screening._manifest(
                connection, self.screening._root(connection, repository, study["search_id"])
            )
            if budget_stop is not None:
                budget_stop = admission(
                    source.budget, self._consumption(connection, study), "reduce"
                )
            if source.code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "候选锁不能更换冻结代码")
            summary = summarize_reduction(state, budget_stop=budget_stop)
            summary_obj = self.history.objects.put_json(
                ObjectType.BLOB, summary.model_dump(mode="json")
            )
            event_id, event_ref = self._event(
                connection,
                repository,
                identity,
                job["revision"] + 1,
                "final_candidate_locked",
                {
                    "summary_ref": summary_obj.id,
                    "state_ref": job["state_ref"],
                    "budget_stop": budget_stop.model_dump(mode="json") if budget_stop else None,
                },
            )
            lock = lock_reduced_candidate(
                state, selection_event_ref=event_ref, budget_stop=budget_stop
            )
            obj = self.history.objects.put_json(ObjectType.BLOB, lock.model_dump(mode="json"))
            for role, body in (("final_lock", obj), ("reduction_summary", summary_obj)):
                self.history._ref(connection, repository, body, "repair_study", str(identity), role)
            connection.execute(
                TABLES["repair_study_locks"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repository,
                    lock_ref=obj.id,
                    summary_ref=summary_obj.id,
                    selection_event_id=event_id,
                )
            )
            connection.execute(
                TABLES["repair_study_jobs"]
                .update()
                .where(TABLES["repair_study_jobs"].c.study_id == identity)
                .values(
                    revision=job["revision"] + 1,
                    status="awaiting_samples",
                    stop_reason="candidate_locked_confirmation_not_opened",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            )
        return self.view(repository, identity)

    def cancel(
        self,
        repository: UUID,
        study_id: UUID,
        *,
        actor: str,
        connection: sa.Connection | None = None,
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "取消必须记录有效操作者")
        with transaction(self.history.engine, connection) as connection:
            study = self._root(connection, repository, study_id)
            job = self._job(connection, repository, study_id)
            if (job["status"] in STUDY_TERMINAL and job["status"] != "needs_attention") or job[
                "cancel_requested"
            ]:
                return {
                    "study_id": str(study_id),
                    "status": job["status"],
                    "cancel_requested": job["cancel_requested"],
                }
            active = self._consumption(connection, study).active
            values: dict[str, Any] = dict(
                cancel_requested=True,
                status="cancelling" if active else "cancelled",
                stop_reason="operator_cancelled",
            )
            if not active:
                values.update(lease_token=None, lease_worker=None, lease_until=None)
            self._update(
                connection, job, "cancel_requested", {"actor": actor, "active": active}, **values
            )
            return {"study_id": str(study_id), "status": values["status"], "cancel_requested": True}

    def view(self, repository: UUID, study_id: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            study = self._root(connection, repository, study_id)
            manifest = self._manifest(connection, study)
            job = self._job(connection, repository, study_id)
            state = self._state(connection, job)
            usage = self._consumption(connection, study)
            trials = [
                dict(row)
                for row in connection.execute(
                    sa.select(TABLES["repair_reduction_trials"])
                    .where(TABLES["repair_reduction_trials"].c.study_id == study_id)
                    .order_by(TABLES["repair_reduction_trials"].c.ordinal)
                ).mappings()
            ]
            details = []
            for row in trials:
                intent = self.owned(
                    connection,
                    repository,
                    row["intent_ref"],
                    "repair_reduction_trial",
                    row["id"],
                    "intent",
                    RepairReductionIntent,
                )
                if (
                    intent.protocol_ref != row["protocol_ref"]
                    or validation_ref(intent.deletion.after) != row["candidate_ref"]
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "缩减意图与索引不同")
                detail: dict[str, Any] = {
                    "trial_id": str(row["id"]),
                    "intent_ref": row["intent_ref"],
                    "intent": intent.model_dump(mode="json"),
                }
                dispatch = (
                    connection.execute(
                        sa.select(TABLES["repair_reduction_jobs"]).where(
                            TABLES["repair_reduction_jobs"].c.id == row["id"]
                        )
                    )
                    .mappings()
                    .first()
                )
                if dispatch is not None:
                    detail["dispatch"] = {
                        "job_id": str(dispatch["job_id"]),
                        "initial_run_id": str(dispatch["initial_run_id"]),
                    }
                result = (
                    connection.execute(
                        sa.select(TABLES["repair_reduction_results"]).where(
                            TABLES["repair_reduction_results"].c.id == row["id"]
                        )
                    )
                    .mappings()
                    .first()
                )
                if result is not None:
                    receipt = self.owned(
                        connection,
                        repository,
                        result["result_ref"],
                        "repair_reduction_trial",
                        row["id"],
                        "result",
                        RepairReductionReceipt,
                    )
                    if receipt.trial_id != str(row["id"]) or any(
                        getattr(receipt.usage, n) != result[n]
                        for n in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                    ):
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "物理缩减收据与索引不同")
                    detail["result"] = {
                        **receipt.model_dump(mode="json"),
                        "result_ref": result["result_ref"],
                        "evidence_ref": result["evidence_ref"],
                    }
                    if not receipt.not_started:
                        evidence = self.owned(
                            connection,
                            repository,
                            result["evidence_ref"],
                            "repair_reduction_trial",
                            row["id"],
                            "evidence",
                            ValidationTrialEvidence,
                        )
                        step = next(
                            (s for s in state.steps if s.evidence.trial_id == str(row["id"])), None
                        )
                        if (
                            step is None
                            or step.evidence != evidence
                            or evidence.result_ref != result["result_ref"]
                        ):
                            raise AgentGitError(
                                ErrorCode.OBJECT_CORRUPT, "删除状态与独立试验结果不符"
                            )
                waiting = (
                    connection.execute(
                        sa.select(TABLES["repair_reduction_waits"]).where(
                            TABLES["repair_reduction_waits"].c.id == row["id"]
                        )
                    )
                    .mappings()
                    .first()
                )
                if waiting is not None:
                    detail["waiting"] = {
                        "reason": waiting["reason"],
                        "payload_ref": waiting["payload_ref"],
                    }
                details.append(detail)
            locked = (
                connection.execute(
                    sa.select(TABLES["repair_study_locks"]).where(
                        TABLES["repair_study_locks"].c.id == study_id
                    )
                )
                .mappings()
                .first()
            )
            lock_view = None
            reduction = summarize_reduction(state)
            if locked is not None:
                lock = self.owned(
                    connection,
                    repository,
                    locked["lock_ref"],
                    "repair_study",
                    study_id,
                    "final_lock",
                    FinalRepairLock,
                )
                lock_view = {
                    "lock_ref": locked["lock_ref"],
                    "summary_ref": locked["summary_ref"],
                    "lock": lock.model_dump(mode="json"),
                }
                reduction = self.owned(
                    connection,
                    repository,
                    locked["summary_ref"],
                    "repair_study",
                    study_id,
                    "reduction_summary",
                    ReductionSummary,
                )
            if job["status"] in STUDY_TERMINAL:
                ended = connection.execute(
                    sa.select(TABLES["repair_study_events"].c.created_at).where(
                        TABLES["repair_study_events"].c.study_id == study_id,
                        TABLES["repair_study_events"].c.revision == job["revision"],
                    )
                ).scalar_one()
                source = self.screening._root(connection, repository, study["search_id"])
                usage = evolve(
                    usage,
                    elapsed_wall_ms=max(
                        0, int((ended - source["created_at"]).total_seconds() * 1000)
                    ),
                )
            response = dict(
                study_id=str(study_id),
                search_id=str(study["search_id"]),
                manifest_ref=study["manifest_ref"],
                manifest=manifest.model_dump(mode="json"),
                status=job["status"],
                revision=job["revision"],
                cancel_requested=job["cancel_requested"],
                stop_reason=job["stop_reason"],
                state_ref=job["state_ref"],
                state=state.model_dump(mode="json"),
                reduction=reduction.model_dump(mode="json"),
                usage=usage.model_dump(mode="json"),
                trials=details,
                final_lock=lock_view,
            )
            if manifest.field_policy is not None:
                from agentgit.services.repair_fields import RepairFieldService

                response["field_reduction"] = RepairFieldService(self.history).view(
                    repository, study_id, connection=connection
                )
            if len(encode(response)) > 8 * 1024 * 1024:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "缩减查询超出有界大小")
            return response
