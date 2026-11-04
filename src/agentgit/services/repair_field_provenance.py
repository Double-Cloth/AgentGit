"""字段试验只接受本 Job 的实际模型请求与自身恢复来源。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.adapters.model_protocol import configured_model_signature
from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import RepairSearchManifest
from agentgit.core.repair_confirmation import ValidationModelCall
from agentgit.core.repair_fields import RepairFieldIntent, RepairFieldProof
from agentgit.core.repair_validation import validation_ref
from agentgit.core.research import ModelRequest
from agentgit.experiments.validation import frozen_inputs
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


def field_execution_proof(
    research: ResearchService,
    repo: UUID,
    identity: UUID,
    source: RepairSearchManifest,
    intent: RepairFieldIntent,
    job: dict[str, Any],
    initial: UUID,
) -> RepairFieldProof:
    history = research.history
    payload = JobService(research).payload(job)
    validator = ValidatorService(history).manifest(repo, intent.request.validator_ref)
    goal, _ = frozen_inputs(history.objects, validator)
    if (
        research.config(repo, UUID(payload.config_id)) != intent.config
        or payload.goal_ref != intent.request.after.task_ref
        or payload.goal_ref != validator.input_ref
        or payload.corpus_digest != validator.corpus_digest
        or payload.evaluation_time != validator.evaluation_time
        or payload.code_revision != source.code_revision
        or payload.limits != intent.limits
        or payload.replay is None
        or payload.replay.mode != ("hybrid" if source.hybrid else "live")
        or payload.replay.source_run_id != source.space.binding.failed_run_id
        or payload.replay.from_commit != source.space.binding.base_commit
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "实际字段 Job 没有使用冻结输入、预算和投影")
    with history.engine.connect() as connection:
        family = [job["run_id"]]
        while family[-1] != initial:
            row = history._run(connection, repo, family[-1])
            parent = row["origin_run_id"]
            if parent is None or parent in family or len(family) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "字段恢复链没有止于本次独立初始 Run")
            family.append(parent)
        table = TABLES["external_calls"]
        rows = (
            connection.execute(
                sa.select(table)
                .where(
                    table.c.repository_id == repo,
                    table.c.run_id.in_(family),
                    table.c.provider.like("llm:%"),
                    table.c.status.in_(("started", "succeeded", "failed", "outcome_unknown")),
                )
                .order_by(table.c.id)
                .limit(1001)
            )
            .mappings()
            .all()
        )
    if len(rows) > 1000:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "字段模型调用来源超出上限")
    calls = []
    for row in rows:
        captured = parse_contract(
            history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent
        )
        request = parse_contract(
            history.objects.get(captured.request_ref, ObjectType.BLOB), ModelRequest
        )
        if (
            captured.occurrence.provider != "llm:" + intent.config.model
            or captured.occurrence.signature_digest != configured_model_signature(intent.config)
            or captured.request_ref != row["request_digest"]
            or request.state.goal != goal
            or request.evaluation_time != validator.evaluation_time
            or request.prompt != intent.config.prompt
            or request.available_tools != intent.config.tools
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "字段原始模型请求与固定条件不同")
        calls.append(
            ValidationModelCall(
                call_id=str(row["id"]),
                run_id=str(row["run_id"]),
                origin_call_id=str(row["origin_call_id"]) if row["origin_call_id"] else None,
                intent_ref=row["intent_ref"],
                result_ref=row["result_ref"],
            )
        )
    return RepairFieldProof(
        trial_id=str(identity),
        protocol_ref=intent.protocol_ref,
        candidate_ref=validation_ref(intent.request.after),
        job_id=str(job["id"]),
        initial_run_id=str(initial),
        current_run_id=str(job["run_id"]),
        run_ids=tuple(str(run) for run in family),
        model_calls=tuple(calls),
    )
