"""把锁定补丁投影到预注册起点，并核对实际模型调用只来自本次 Job。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.adapters.model_protocol import configured_model_signature
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import RepairSearchManifest
from agentgit.core.repair_confirmation import (
    RepairValidationIntent,
    RepairValidationProjection,
    ValidationExecutionProof,
    ValidationModelCall,
)
from agentgit.core.repair_pools import RegisteredValidationSample
from agentgit.core.repair_validation import FinalRepairLock, validation_ref
from agentgit.core.research import AgentConfig, BudgetLimits, ModelRequest, evolve
from agentgit.experiments.validation import frozen_inputs
from agentgit.services.hybrid import freeze_policy
from agentgit.services.jobs import JobService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


def prepare_projection(
    research: ResearchService,
    repository: UUID,
    source: RepairSearchManifest,
    lock: FinalRepairLock,
    registered: RegisteredValidationSample,
    limits: BudgetLimits,
    *,
    study_id: UUID,
) -> RepairValidationProjection:
    history, ticket = research.history, registered.ticket
    view = VersionService(history).checkout(repository, ticket.from_commit)
    validator = ValidatorService(history).manifest(repository, ticket.validator_ref)
    goal, corpus = frozen_inputs(history.objects, validator)
    config = parse_contract(
        history.objects.get(ticket.source_config_ref, ObjectType.BLOB), AgentConfig
    )
    captured = history.objects.get_json(view["environment"]["input_ref"], ObjectType.BLOB)
    if (
        view["manifest"]["state_ref"] != ticket.source_state_ref
        or view["manifest"]["environment_ref"] != ticket.environment_ref
        or parse_contract(encode(captured["config"]), AgentConfig) != config
        or validator.input_ref != ticket.task_ref
        or validator.corpus_ref != ticket.corpus_ref
        or validator.fixture_split != registered.sample.role
        or validation_ref(goal) != ticket.task_ref
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "采样条件或起点与预注册票据不同")
    model_conditions = history.objects.get_json(ticket.model_conditions_ref, ObjectType.BLOB)
    if model_conditions != dict(
        schema_version=1,
        kind="repair_model_conditions/v1",
        model=config.model,
        openai=config.openai.model_dump(mode="json") if config.openai else None,
        generation="fresh_model_invocations/v1",
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "冻结模型生成条件不能被替换")
    applied = RepairStudyService(history)._applied(
        repository, source, lock.candidate, study_id=study_id
    )
    if applied.config is not None:
        changed = evolve(config, prompt=applied.config.prompt)
        if changed != applied.config:
            raise AgentGitError(ErrorCode.FORBIDDEN, "补丁只能投影原已锁定的 Prompt 字段")
        config = changed
    if not limits.within(config.limits):
        raise AgentGitError(ErrorCode.FORBIDDEN, "验证试验不得扩大预注册配置授权")
    hybrid_ref = None
    observed = tuple(
        a.patch_ref for a in lock.candidate.atoms if a.operator == "TOOL_RESPONSE_PATCH"
    )
    if registered.sample.role == "confirm" and source.hybrid is not None:
        hybrid_ref = freeze_policy(
            history,
            repository,
            ticket.source_run_id,
            ticket.from_commit,
            ticket.validator_ref,
            corpus,
            source.hybrid,
            applied.replacements,
        ).id
    scope = (
        "selected_source"
        if observed and registered.sample.role == "confirm"
        else ("outside_source" if observed else "none")
    )
    return RepairValidationProjection.model_validate(
        dict(
            lock_ref=validation_ref(lock),
            ticket_ref=validation_ref(ticket),
            config=evolve(config, limits=limits),
            plan=applied.plan,
            hybrid_ref=hybrid_ref,
            observation_scope=scope,
            unapplied_observation_atoms=observed if scope == "outside_source" else (),
        )
    )


def execution_proof(
    research: ResearchService,
    repository: UUID,
    identity: UUID,
    intent: RepairValidationIntent,
    projection: RepairValidationProjection,
    job: dict[str, Any],
    initial_run: UUID,
) -> ValidationExecutionProof:
    history, ticket = research.history, intent.registered.ticket
    queue = JobService(research)
    payload = queue.payload(job)
    if (
        research.config(repository, UUID(payload.config_id)) != projection.config
        or payload.goal_ref != ticket.task_ref
        or payload.code_revision != ticket.code_revision
        or payload.replay is None
        or payload.replay.mode != ticket.execution_mode
        or payload.replay.source_run_id != ticket.source_run_id
        or payload.replay.from_commit != ticket.from_commit
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "实际 Job 与冻结采样或投影条件不同")
    validator = ValidatorService(history).manifest(repository, ticket.validator_ref)
    goal, _ = frozen_inputs(history.objects, validator)
    with history.engine.connect() as connection:
        family = [job["run_id"]]
        while family[-1] != initial_run:
            run = history._run(connection, repository, family[-1])
            parent = run["origin_run_id"]
            if parent is None or parent in family or len(family) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "验证恢复链没有止于本次独立初始 Run")
            family.append(parent)
        calls = TABLES["external_calls"]
        rows = (
            connection.execute(
                sa.select(calls)
                .where(
                    calls.c.repository_id == repository,
                    calls.c.run_id.in_(family),
                    calls.c.provider.like("llm:%"),
                    calls.c.status.in_(("started", "succeeded", "failed", "outcome_unknown")),
                )
                .order_by(calls.c.id)
                .limit(1001)
            )
            .mappings()
            .all()
        )
    if len(rows) > 1000:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "验证 Job 模型调用来源超过上限")
    ids = {row["id"] for row in rows}
    by_id = {row["id"]: row for row in rows}
    frozen = []
    for row in rows:
        parent, seen = row["origin_call_id"], {row["id"]}
        while parent is not None:
            if parent not in ids or parent in seen:
                raise AgentGitError(ErrorCode.FORBIDDEN, "验证不能借用其他 Job 的模型响应")
            seen.add(parent)
            parent = by_id[parent]["origin_call_id"]
        captured = parse_contract(
            history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent
        )
        request = parse_contract(
            history.objects.get(captured.request_ref, ObjectType.BLOB), ModelRequest
        )
        if (
            captured.occurrence.provider != "llm:" + projection.config.model
            or captured.occurrence.signature_digest != configured_model_signature(projection.config)
            or captured.request_ref != row["request_digest"]
            or request.state.goal != goal
            or request.prompt != projection.config.prompt
            or request.available_tools != projection.config.tools
            or request.evaluation_time != validator.evaluation_time
        ):
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "实际模型请求与预注册任务/模型/权限/时钟不同"
            )
        frozen.append(
            ValidationModelCall(
                call_id=str(row["id"]),
                run_id=str(row["run_id"]),
                origin_call_id=str(row["origin_call_id"]) if row["origin_call_id"] else None,
                intent_ref=row["intent_ref"],
                result_ref=row["result_ref"],
            )
        )
    return ValidationExecutionProof(
        trial_id=str(identity),
        sample_id=intent.sample_id,
        protocol_ref=intent.protocol_ref,
        projection_ref=intent.projection_ref,
        job_id=str(job["id"]),
        initial_run_id=str(initial_run),
        current_run_id=str(job["run_id"]),
        run_ids=tuple(str(run) for run in family),
        model_calls=tuple(frozen),
    )
