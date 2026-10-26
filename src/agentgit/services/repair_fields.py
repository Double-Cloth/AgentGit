"""锁前字段控制与不可变来源；实际模型、工具仍由普通 Worker 执行。"""

from contextlib import nullcontext
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ValidatorManifest
from agentgit.core.models import CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.repair import BudgetDecision, RepairPatch, RepairSearchManifest
from agentgit.core.repair_fields import (
    RepairFieldIntent,
    RepairFieldPlan,
    RepairFieldProof,
    RepairFieldReceipt,
    RepairFieldRequest,
    RepairFieldState,
    RepairFieldSummary,
    RepairFieldVariant,
)
from agentgit.core.repair_studies import RepairStudyLease
from agentgit.core.repair_validation import ReductionState, ValidationTrialEvidence, validation_ref
from agentgit.core.research import AgentConfig, AgentState, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import LiveOverrides
from agentgit.experiments.validation import frozen_inputs
from agentgit.repair.budget import admission
from agentgit.repair.field_reduction import (
    lock_field_candidate,
    next_field_trial,
    replay_fields,
    summarize_fields,
)
from agentgit.repair.reduction import summarize_reduction
from agentgit.repair.space import patch_ref
from agentgit.replay.live import prepare_live
from agentgit.services.environment import source_revision
from agentgit.services.history import HistoryService
from agentgit.services.hybrid import freeze_policy
from agentgit.services.interventions import InterventionService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


class RepairFieldService:
    def __init__(self, history: HistoryService):
        self.history, self.ledger = history, RepairStudyService(history)

    def source(self, connection: sa.Connection, repo: UUID, study: UUID) -> RepairSearchManifest:
        row = self.ledger._root(connection, repo, study)
        return self.ledger.screening._manifest(
            connection, self.ledger.screening._root(connection, repo, row["search_id"])
        )

    def make_plan(
        self,
        repo: UUID,
        source: RepairSearchManifest,
        state: ReductionState,
        *,
        connection: sa.Connection,
        parent_budget_stop: BudgetDecision | None = None,
    ) -> RepairFieldPlan:
        summary = summarize_reduction(state, budget_stop=parent_budget_stop)
        if summary.status in ("running", "needs_review", "cancelled"):
            raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "原子删除没有排空或缺少确定证据")
        row = self.history._commit_row(connection, repo, source.space.binding.base_commit)
        commit = parse_contract(
            self.history.objects.get(source.space.binding.base_commit, ObjectType.COMMIT),
            CommitManifest,
        )
        if (
            commit.repository_id != str(repo)
            or commit.environment_ref != source.space.binding.environment_ref
            or any(
                getattr(commit, key) != row[key]
                for key in ("state_ref", "environment_ref", "event_manifest_ref")
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段来源检查点没有绑定同项目冻结输入")
        environment = parse_contract(
            self.history.objects.get(commit.environment_ref, ObjectType.ENVIRONMENT),
            EnvironmentManifest,
        )
        snapshot = parse_contract(
            self.history.objects.get(commit.state_ref, ObjectType.STATE), StateSnapshot
        )
        frozen = self.history.objects.get_json(environment.input_ref, ObjectType.BLOB)
        original = parse_contract(encode(snapshot.data), AgentState)
        validator = parse_contract(
            self.history.objects.get(state.sample.validator_ref, ObjectType.BLOB), ValidatorManifest
        )
        if (
            frozen.get("goal") != original.goal.model_dump(mode="json")
            or object_digest(ObjectType.BLOB, encode(original.goal.model_dump(mode="json")))
            != state.sample.task_ref
            or validator.input_ref != state.sample.task_ref
            or frozen.get("corpus_digest") != validator.corpus_digest
            or environment.evaluation_time != validator.evaluation_time
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "字段来源任务、语料或时钟不同于原验收器")
        return RepairFieldPlan(
            parent_reduction_ref=validation_ref(state),
            initial=summary.final_candidate,
            source_config=parse_contract(encode(frozen["config"]), AgentConfig),
            source_plan=original.plan,
            completed=original.completed,
            patches=source.space.patches,
            sample=state.sample,
            initial_atomic_minimality=summary.minimality,
            prior_run_ids=(*state.screening_run_ids, *(s.evidence.run_id for s in state.steps)),
            prior_result_refs=(
                *state.screening_result_refs,
                *(s.evidence.result_ref for s in state.steps),
            ),
            max_trials=0 if parent_budget_stop else max(0, state.max_trials - len(state.steps)),
            parent_budget_stop=parent_budget_stop,
        )

    def state(
        self, connection: sa.Connection, repo: UUID, study: UUID, job: Any | None = None
    ) -> RepairFieldState | None:
        job = job if job is not None else self.ledger._job(connection, repo, study)
        reference = job["field_state_ref"]
        if reference is None:
            return None
        value = self.ledger.owned(
            connection, repo, reference, "repair_study", study, "field_state", RepairFieldState
        )
        old = self.ledger._state(connection, job)
        source = self.source(connection, repo, study)
        root = self.ledger._root(connection, repo, study)
        if root["field_policy"] != value.plan.policy or value.plan != self.make_plan(
            repo,
            source,
            old,
            connection=connection,
            parent_budget_stop=value.plan.parent_budget_stop,
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段计划不能更换来源、范围或剩余额度")
        replay_fields(value)
        table = TABLES["repair_field_states"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.study_id == study,
                    table.c.repository_id == repo,
                    table.c.state_ref == reference,
                )
            )
            .mappings()
            .one()
        )
        if row["sequence"] != len(value.steps):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段索引没有绑定完整轨迹前缀")
        return value

    def advance(
        self,
        connection: sa.Connection,
        repo: UUID,
        study: UUID,
        job: Any,
        state: RepairFieldState,
        action: str,
        details: dict[str, Any],
        **updates: Any,
    ) -> str:
        obj = self.history.objects.put_json(ObjectType.BLOB, state.model_dump(mode="json"))
        self.history._ref(connection, repo, obj, "repair_study", str(study), "field_state")
        event, _ = self.ledger._event(
            connection, repo, study, job["revision"] + 1, action, {**details, "state_ref": obj.id}
        )
        connection.execute(
            TABLES["repair_field_states"]
            .insert()
            .values(
                repository_id=repo,
                study_id=study,
                state_ref=obj.id,
                parent_state_ref=job["field_state_ref"],
                sequence=len(state.steps),
                event_id=event,
            )
        )
        connection.execute(
            TABLES["repair_study_jobs"]
            .update()
            .where(TABLES["repair_study_jobs"].c.study_id == study)
            .values(field_state_ref=obj.id, revision=job["revision"] + 1, **updates)
        )
        return obj.id

    def initialize(
        self, lease: RepairStudyLease, *, budget_stopped: bool = False
    ) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            root = self.ledger._root(connection, repo, study)
            if root["field_policy"] != "predefined-fields/v1":
                raise AgentGitError(ErrorCode.FORBIDDEN, "旧 study 没有冻结字段策略，不能事后添加")
            current = self.state(connection, repo, study, job)
            if current is not None:
                return dict(initialized=True, reused=True)
            if job["cancel_requested"] or self.ledger._consumption(connection, root).active:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消或原子未决时不能初始化字段")
            source = self.source(connection, repo, study)
            if source.code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码初始化旧来源字段")
            denied = None
            if budget_stopped:
                denied = admission(
                    source.budget, self.ledger._consumption(connection, root), "reduce"
                )
                if denied.allowed or denied.reason == "concurrency":
                    raise AgentGitError(ErrorCode.VALIDATION, "没有实际预算拒绝，不能跳过原子阶段")
            state = RepairFieldState(
                plan=self.make_plan(
                    repo,
                    source,
                    self.ledger._state(connection, job),
                    connection=connection,
                    parent_budget_stop=denied,
                )
            )
            self.advance(connection, repo, study, job, state, "field_started", {})
        return dict(initialized=True, reused=False)

    def resolve_patch(
        self,
        connection: sa.Connection,
        repo: UUID,
        study: UUID,
        source: RepairSearchManifest,
        reference: str,
    ) -> tuple[RepairPatch, str]:
        table, studies, searches = (
            TABLES["repair_field_patches"],
            TABLES["repair_studies"],
            TABLES["repair_searches"],
        )
        row = (
            connection.execute(
                sa.select(table)
                .join(studies, studies.c.id == table.c.study_id)
                .join(searches, searches.c.id == studies.c.search_id)
                .where(
                    table.c.repository_id == repo,
                    table.c.study_id == study,
                    studies.c.repository_id == repo,
                    searches.c.repository_id == repo,
                    table.c.effect_ref == reference,
                    table.c.source_manifest_ref == validation_ref(source),
                    searches.c.manifest_ref == table.c.source_manifest_ref,
                    studies.c.field_policy == "predefined-fields/v1",
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "字段变体不属于当前 study 的冻结来源")
        variant = self.ledger.owned(
            connection,
            repo,
            row["variant_ref"],
            "repair_field_patch",
            row["id"],
            "variant",
            RepairFieldVariant,
        )
        if (
            patch_ref(variant.patch) != reference
            or variant.root_patch_ref != row["root_patch_ref"]
            or variant.parent_patch_ref != row["parent_patch_ref"]
            or variant.root_patch_ref not in {patch_ref(p) for p in source.space.patches}
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "派生补丁效果或根来源索引失配")
        definition, payload = InterventionService(self.history).get(
            repo, row["intervention_id"], connection=connection
        )
        expected, actual = (
            variant.patch.intervention.model_dump(mode="json"),
            definition.model_dump(mode="json"),
        )
        if payload != variant.patch.intervention.payload or any(
            actual.get(k) != v for k, v in expected.items() if k != "payload"
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "派生效果与实际发布的 S7 内容不同")
        return variant.patch, str(row["intervention_id"])

    def trial(
        self, connection: sa.Connection, repo: UUID, study: UUID, identity: UUID
    ) -> tuple[Any, RepairFieldIntent]:
        table = TABLES["repair_field_trials"]
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
            raise AgentGitError(ErrorCode.NOT_FOUND, "字段试验不可访问")
        intent = self.ledger.owned(
            connection,
            repo,
            row["intent_ref"],
            "repair_field_trial",
            identity,
            "intent",
            RepairFieldIntent,
        )
        if (
            intent.study_id != str(study)
            or intent.ordinal != row["ordinal"]
            or intent.protocol_ref != row["protocol_ref"]
            or intent.request.kind != row["kind"]
            or validation_ref(intent.request.after) != row["candidate_ref"]
            or intent.branch_name != row["branch_name"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段意图与索引不同")
        prefix = self.ledger.owned(
            connection,
            repo,
            intent.protocol_ref,
            "repair_study",
            study,
            "field_state",
            RepairFieldState,
        )
        source = self.source(connection, repo, study)
        if next_field_trial(
            prefix
        ) != intent.request or intent.source_manifest_ref != validation_ref(source):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段派发没有使用唯一冻结后继")
        replayed = replay_fields(prefix)
        patches = dict(replayed.patches)
        if intent.request.variant is not None:
            patches[patch_ref(intent.request.variant.patch)] = intent.request.variant.patch
        config, plan = evolve(prefix.plan.source_config, limits=source.budget.trial), None
        for atom in intent.request.after.atoms:
            payload = patches[atom.patch_ref].intervention.payload
            if payload.kind == "PROMPT_PATCH":
                config = evolve(config, prompt=payload.prompt)
            elif payload.kind == "PLAN_PATCH":
                plan = payload.plan
        if intent.config != config or intent.plan != plan or intent.limits != source.budget.trial:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段投影不能改写授权或确切补丁内容")
        return row, intent

    def dispatch_row(self, connection: sa.Connection, identity: UUID) -> Any:
        table = TABLES["repair_field_jobs"]
        return connection.execute(sa.select(table).where(table.c.id == identity)).mappings().first()

    def result(
        self, connection: sa.Connection, repo: UUID, identity: UUID
    ) -> tuple[Any, RepairFieldReceipt] | None:
        table = TABLES["repair_field_results"]
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
            "repair_field_trial",
            identity,
            "result",
            RepairFieldReceipt,
        )
        execution = receipt.execution
        if (
            execution.trial_id != str(identity)
            or execution.verdict != row["verdict"]
            or execution.not_started != row["not_started"]
            or receipt.provenance_ref != row["provenance_ref"]
            or any(
                getattr(execution.usage, n) != row[n]
                for n in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段实际收据与索引不同")
        return row, receipt

    @staticmethod
    def evidence(receipt: RepairFieldReceipt, reference: str) -> ValidationTrialEvidence:
        value = receipt.execution
        return ValidationTrialEvidence.model_validate(
            dict(
                phase="reduce",
                protocol_ref=value.protocol_ref,
                trial_id=value.trial_id,
                run_id=value.current_run_id,
                result_ref=reference,
                candidate_ref=value.candidate_ref,
                sample_ref=value.sample_ref,
                validator_ref=value.validator_ref,
                verdict=value.verdict,
                category=value.category,
                usage=value.usage,
                safety_violations=value.safety_violations,
            )
        )

    def records(
        self, connection: sa.Connection, repo: UUID, study: UUID, state: RepairFieldState
    ) -> list[dict[str, Any]]:
        table = TABLES["repair_field_trials"]
        ids = (
            connection.execute(
                sa.select(table.c.id)
                .where(table.c.study_id == study, table.c.repository_id == repo)
                .order_by(table.c.ordinal)
            )
            .scalars()
            .all()
        )
        details = []
        steps = {step.evidence.trial_id: step for step in state.steps}
        observed_steps = set()
        for identity in ids:
            row, intent = self.trial(connection, repo, study, identity)
            detail = dict(
                trial_id=str(identity),
                intent_ref=row["intent_ref"],
                intent=intent.model_dump(mode="json"),
            )
            dispatched, saved = (
                self.dispatch_row(connection, identity),
                self.result(connection, repo, identity),
            )
            if dispatched is not None:
                detail["dispatch"] = dict(
                    job_id=str(dispatched["job_id"]),
                    initial_run_id=str(dispatched["initial_run_id"]),
                )
            if saved is not None:
                result, receipt = saved
                execution = receipt.execution
                if (
                    execution.study_id != str(study)
                    or execution.protocol_ref != intent.protocol_ref
                    or execution.candidate_ref != validation_ref(intent.request.after)
                    or execution.sample_ref != intent.request.sample_ref
                    or execution.validator_ref != intent.request.validator_ref
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段收据换了候选、协议或验收器")
                if execution.not_started:
                    if dispatched is not None or str(identity) in steps:
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "空取消不能有实际字段证据")
                else:
                    if (
                        dispatched is None
                        or execution.job_id != str(dispatched["job_id"])
                        or execution.initial_run_id != str(dispatched["initial_run_id"])
                    ):
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段收据没有绑定独立派发")
                    if receipt.provenance_ref is None:
                        raise AgentGitError(
                            ErrorCode.OBJECT_CORRUPT, "实际字段试验缺少调用来源证明"
                        )
                    proof = self.ledger.owned(
                        connection,
                        repo,
                        receipt.provenance_ref,
                        "repair_field_trial",
                        identity,
                        "provenance",
                        RepairFieldProof,
                    )
                    evidence = self.ledger.owned(
                        connection,
                        repo,
                        result["evidence_ref"],
                        "repair_field_trial",
                        identity,
                        "evidence",
                        ValidationTrialEvidence,
                    )
                    step = steps.get(str(identity))
                    if (
                        proof.trial_id != str(identity)
                        or proof.protocol_ref != intent.protocol_ref
                        or proof.candidate_ref != execution.candidate_ref
                        or proof.job_id != execution.job_id
                        or proof.initial_run_id != execution.initial_run_id
                        or proof.current_run_id != execution.current_run_id
                        or sum(c.origin_call_id is None for c in proof.model_calls)
                        != receipt.fresh_model_calls
                        or step is None
                        or step.request != intent.request
                        or step.evidence != evidence
                        or evidence != self.evidence(receipt, result["result_ref"])
                    ):
                        raise AgentGitError(
                            ErrorCode.OBJECT_CORRUPT, "字段协议与实际调用/结果证据不同"
                        )
                    observed_steps.add(str(identity))
                detail["result"] = {
                    **receipt.model_dump(mode="json"),
                    "result_ref": result["result_ref"],
                    "evidence_ref": result["evidence_ref"],
                }
            marker = TABLES["repair_field_waits"]
            waiting = (
                connection.execute(sa.select(marker).where(marker.c.id == identity))
                .mappings()
                .first()
            )
            if waiting is not None:
                detail["waiting"] = dict(
                    reason=waiting["reason"], payload_ref=waiting["payload_ref"]
                )
            details.append(detail)
        if observed_steps != set(steps):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段轨迹包含没有实际结果索引的试验")
        return details

    def _projection(
        self, repo: UUID, study: UUID, source: RepairSearchManifest, request: RepairFieldRequest
    ) -> tuple[AgentConfig, Any, Any]:
        variant = request.variant
        other = (
            request.after
            if variant is None
            else evolve(
                request.after,
                atoms=tuple(
                    atom
                    for atom in request.after.atoms
                    if atom.patch_ref != patch_ref(variant.patch)
                ),
            )
        )
        applied = self.ledger._applied(repo, source, other, study_id=study)
        base = VersionService(self.history).checkout(repo, source.space.binding.base_commit)
        frozen = self.history.objects.get_json(base["environment"]["input_ref"], ObjectType.BLOB)
        config = applied.config or parse_contract(encode(frozen["config"]), AgentConfig)
        plan = applied.plan
        if variant is not None:
            payload = variant.patch.intervention.payload
            if payload.kind == "PROMPT_PATCH":
                config = evolve(config, prompt=payload.prompt)
            elif payload.kind == "PLAN_PATCH":
                plan = payload.plan
            else:
                raise AgentGitError(ErrorCode.FORBIDDEN, "字段策略不能改写工具观测")
        config = evolve(config, limits=source.budget.trial)
        prepare_live(
            parse_contract(encode(base["snapshot"]), StateSnapshot),
            config,
            LiveOverrides(config=config, plan=plan),
            self.history.objects.get,
        )
        hybrid = None
        if source.hybrid is not None:
            validator = ValidatorService(self.history).manifest(
                repo, source.space.binding.validator_ref
            )
            _, corpus = frozen_inputs(self.history.objects, validator)
            hybrid = freeze_policy(
                self.history,
                repo,
                source.space.binding.failed_run_id,
                source.space.binding.base_commit,
                source.space.binding.validator_ref,
                corpus,
                source.hybrid,
                applied.replacements,
            )
        return config, plan, hybrid

    def reserve(self, lease: RepairStudyLease) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            if job["cancel_requested"] or job["status"] != "reducing":
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "取消或待核对时不能扩展字段")
            state = self.state(connection, repo, study, job)
            if state is None:
                raise AgentGitError(ErrorCode.VALIDATION, "字段阶段尚未初始化")
            pending = next(
                (
                    item
                    for item in self.records(connection, repo, study, state)
                    if "result" not in item
                ),
                None,
            )
            if pending is not None:
                return dict(admitted=True, reused=True, trial_id=pending["trial_id"])
            request, protocol = next_field_trial(state), job["field_state_ref"]
            if request is None:
                return dict(admitted=False, finished=True)
            source = self.source(connection, repo, study)
            root = self.ledger._root(connection, repo, study)
        if source.code_revision != source_revision():
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码预留旧字段试验")
        prepared = (
            InterventionService(self.history).prepare(
                repo, request.variant.patch.intervention, actor=root["actor"]
            )
            if request.variant
            else None
        )
        config, plan, hybrid = self._projection(repo, study, source, request)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            if (
                job["field_state_ref"] != protocol
                or job["cancel_requested"]
                or job["status"] != "reducing"
            ):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "字段准备期间控制或协议改变")
            if source.code_revision != source_revision():
                raise AgentGitError(
                    ErrorCode.UNSUPPORTED_SCHEMA, "准备期间代码改变，未预留字段试验"
                )
            usage = self.ledger._consumption(connection, root)
            allowed = admission(source.budget, usage, "reduce")
            if not allowed.allowed:
                self.ledger._update(
                    connection,
                    job,
                    "field_budget_denied",
                    allowed.model_dump(mode="json"),
                    stop_reason=allowed.reason,
                )
                return dict(admitted=False, decision=allowed.model_dump(mode="json"))
            variant_id = None
            if request.variant is not None:
                variant = request.variant
                table = TABLES["repair_field_patches"]
                found = (
                    connection.execute(
                        sa.select(table).where(
                            table.c.study_id == study,
                            table.c.effect_ref == patch_ref(variant.patch),
                        )
                    )
                    .mappings()
                    .first()
                )
                if found is not None:
                    self.resolve_patch(connection, repo, study, source, found["effect_ref"])
                    variant_id = found["id"]
                else:
                    assert prepared is not None
                    published = InterventionService(self.history).publish(
                        prepared, connection=connection
                    )
                    variant_id = uuid4()
                    body = self.history.objects.put_json(
                        ObjectType.BLOB, variant.model_dump(mode="json")
                    )
                    self.history._ref(
                        connection, repo, body, "repair_field_patch", str(variant_id), "variant"
                    )
                    connection.execute(
                        table.insert().values(
                            id=variant_id,
                            repository_id=repo,
                            study_id=study,
                            effect_ref=patch_ref(variant.patch),
                            variant_ref=body.id,
                            root_patch_ref=variant.root_patch_ref,
                            parent_patch_ref=variant.parent_patch_ref,
                            source_manifest_ref=validation_ref(source),
                            intervention_id=UUID(published["intervention_id"]),
                        )
                    )
            identity = uuid4()
            intent = RepairFieldIntent(
                study_id=str(study),
                ordinal=usage.started.reduce,
                protocol_ref=protocol,
                source_manifest_ref=validation_ref(source),
                request=request,
                config=config,
                plan=plan,
                limits=source.budget.trial,
                hybrid_ref=hybrid.id if hybrid else None,
                branch_name=f"field-{study.hex}-{usage.started.reduce}",
            )
            for role, value in (("intent", intent), ("candidate", request.after)):
                body = self.history.objects.put_json(ObjectType.BLOB, value.model_dump(mode="json"))
                self.history._ref(connection, repo, body, "repair_field_trial", str(identity), role)
            if hybrid is not None:
                self.history._ref(
                    connection, repo, hybrid, "repair_field_trial", str(identity), "hybrid"
                )
            connection.execute(
                TABLES["repair_field_trials"]
                .insert()
                .values(
                    id=identity,
                    repository_id=repo,
                    study_id=study,
                    ordinal=intent.ordinal,
                    phase="reduce",
                    kind=request.kind,
                    protocol_ref=protocol,
                    candidate_ref=validation_ref(request.after),
                    variant_id=variant_id,
                    intent_ref=validation_ref(intent),
                    intent_key=validation_ref(intent),
                    branch_name=intent.branch_name,
                )
            )
            self.ledger._update(
                connection,
                job,
                "field_trial_reserved",
                dict(trial_id=str(identity)),
                stop_reason=None,
            )
        return dict(admitted=True, reused=False, trial_id=str(identity))

    def seal(self, lease: RepairStudyLease, *, budget_stopped: bool = False) -> dict[str, Any]:
        repo, study = UUID(lease.repository_id), UUID(lease.study_id)
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            root = self.ledger._root(connection, repo, study)
            if job["cancel_requested"] or self.ledger._consumption(connection, root).active:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "字段未排空或已取消时不能锁定")
            state = self.state(connection, repo, study, job)
            if state is None:
                raise AgentGitError(ErrorCode.VALIDATION, "字段状态不存在")
            if self.source(connection, repo, study).code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "不能用新代码封存旧字段状态")
            if budget_stopped:
                decision = admission(
                    self.source(connection, repo, study).budget,
                    self.ledger._consumption(connection, root),
                    "reduce",
                )
                if decision.allowed or decision.reason == "concurrency":
                    raise AgentGitError(ErrorCode.VALIDATION, "实际预算仍允许时不能提前封存字段")
            summary = summarize_fields(state, budget_stopped=budget_stopped)
            body = self.history.objects.put_json(ObjectType.BLOB, summary.model_dump(mode="json"))
            self.history._ref(connection, repo, body, "repair_study", str(study), "field_summary")
            old_summary = summarize_reduction(
                self.ledger._state(connection, job), budget_stop=state.plan.parent_budget_stop
            )
            previous = self.history.objects.put_json(
                ObjectType.BLOB, old_summary.model_dump(mode="json")
            )
            self.history._ref(
                connection, repo, previous, "repair_study", str(study), "reduction_summary"
            )
            event, reference = self.ledger._event(
                connection,
                repo,
                study,
                job["revision"] + 1,
                "final_candidate_locked",
                dict(
                    field_summary_ref=body.id,
                    state_ref=job["field_state_ref"],
                    budget_stopped=budget_stopped,
                ),
            )
            lock = lock_field_candidate(
                state, selection_event_ref=reference, budget_stopped=budget_stopped
            )
            locked = self.history.objects.put_json(ObjectType.BLOB, lock.model_dump(mode="json"))
            self.history._ref(connection, repo, locked, "repair_study", str(study), "final_lock")
            connection.execute(
                TABLES["repair_field_summaries"]
                .insert()
                .values(
                    study_id=study,
                    repository_id=repo,
                    summary_ref=body.id,
                    state_ref=job["field_state_ref"],
                    event_id=event,
                )
            )
            connection.execute(
                TABLES["repair_study_locks"]
                .insert()
                .values(
                    id=study,
                    repository_id=repo,
                    lock_ref=locked.id,
                    summary_ref=previous.id,
                    selection_event_id=event,
                )
            )
            connection.execute(
                TABLES["repair_study_jobs"]
                .update()
                .where(TABLES["repair_study_jobs"].c.study_id == study)
                .values(
                    revision=job["revision"] + 1,
                    status="awaiting_samples",
                    stop_reason="field_candidate_locked_confirmation_not_opened",
                    lease_token=None,
                    lease_worker=None,
                    lease_until=None,
                )
            )
        return self.ledger.view(repo, study)

    def view(
        self, repo: UUID, study: UUID, *, connection: sa.Connection | None = None
    ) -> dict[str, Any] | None:
        with (
            self.history.engine.connect() if connection is None else nullcontext(connection)
        ) as reading:
            return self._view(reading, repo, study)

    def _view(self, connection: sa.Connection, repo: UUID, study: UUID) -> dict[str, Any] | None:
        state = self.state(connection, repo, study)
        if state is None:
            return None
        summary = summarize_fields(state)
        table = TABLES["repair_field_summaries"]
        saved = (
            connection.execute(
                sa.select(table).where(table.c.study_id == study, table.c.repository_id == repo)
            )
            .mappings()
            .first()
        )
        reference = None
        if saved is not None:
            summary = self.ledger.owned(
                connection,
                repo,
                saved["summary_ref"],
                "repair_study",
                study,
                "field_summary",
                RepairFieldSummary,
            )
            reference = saved["summary_ref"]
            if summary.state_ref != validation_ref(state):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段摘要与轨迹不符")
        return dict(
            state_ref=validation_ref(state),
            state=state.model_dump(mode="json"),
            summary=summary.model_dump(mode="json"),
            summary_ref=reference,
            trials=self.records(connection, repo, study, state),
        )
