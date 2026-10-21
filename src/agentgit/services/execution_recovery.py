"""已停止的隔离执行可丢弃临时文件；未知外部写入不能使用此路径。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import SANDBOX_PROVIDERS, ExecutionPolicy, StoppedExecution
from agentgit.core.jobs import Lease
from agentgit.core.models import (
    CallIntent,
    CallOccurrence,
    Contract,
    EffectClass,
    RunStatus,
    parse_contract,
)
from agentgit.core.research import AgentState, evolve
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.managed_calls import ManagedCallContext

if TYPE_CHECKING:
    from agentgit.services.research import ResearchSession


class _CleanupProof(Contract):
    container_name: str
    container_id: str | None
    cleanup_confirmed: Literal[True]
    previous_fencing_token: int
    observer_fencing_token: int


def occurrence_of(call: dict[str, Any]) -> CallOccurrence:
    return CallOccurrence(
        **{
            key: call[key]
            for key in (
                "logical_step_id",
                "call_index",
                "attempt",
                "provider",
                "signature_digest",
                "request_digest",
            )
        }
    )


def _stopped_row(
    connection: Connection,
    lease: Lease,
    runs: tuple[UUID, ...],
    occurrence: CallOccurrence,
    policy: ExecutionPolicy,
) -> tuple[dict[str, Any], str] | None:
    calls, executions, refs = (
        TABLES["external_calls"],
        TABLES["sandbox_executions"],
        TABLES["object_refs"],
    )
    rows = list(
        connection.execute(
            sa.select(executions, calls.c.intent_ref)
            .join(calls, calls.c.id == executions.c.call_id)
            .where(
                executions.c.repository_id == lease.repository_id,
                executions.c.job_id == lease.job_id,
                executions.c.run_id.in_(runs),
                executions.c.fencing_token < lease.fencing_token,
                executions.c.cleanup_confirmed.is_(True),
                executions.c.status == "outcome_unknown",
                executions.c.result_ref.is_(None),
                executions.c.policy_ref == policy.digest(),
                executions.c.image_id == policy.image_id,
                calls.c.repository_id == lease.repository_id,
                calls.c.run_id == executions.c.run_id,
                calls.c.result_ref.is_(None),
                calls.c.status.in_(("started", "outcome_unknown")),
                calls.c.effect == EffectClass.NON_IDEMPOTENT_WRITE.value,
                *(
                    getattr(calls.c, key) == value
                    for key, value in occurrence.model_dump().items()
                    if key != "schema_version"
                ),
            )
            .with_for_update()
        ).mappings()
    )
    if len(rows) != 1:
        return None
    row = dict(rows[0])
    proofs = list(
        connection.execute(
            sa.select(refs.c.object_id).where(
                refs.c.repository_id == lease.repository_id,
                refs.c.owner_type == "sandbox_execution",
                refs.c.owner_id == str(row["call_id"]),
                refs.c.role == "recovery_cleanup",
            )
        ).scalars()
    )
    return (row, proofs[0]) if len(proofs) == 1 else None


def retry_at_checkpoint(
    history: HistoryService, context: ManagedCallContext, policy: ExecutionPolicy
) -> StoppedExecution:
    occurrence = context.occurrence
    if context.checkpoint is None or not 2 <= occurrence.attempt <= policy.max_attempts:
        raise AgentGitError(ErrorCode.FORBIDDEN, "新执行尝试超出固定策略或缺少检查点")
    state = parse_contract(
        encode(history.restore(context.repository_id, context.checkpoint).data), AgentState
    )
    previous = occurrence.model_copy(update={"attempt": occurrence.attempt - 1})
    candidates = [item for item in state.stopped_executions if item.occurrence == previous]
    if (
        len(candidates) != 1
        or state.status != RunStatus.RUNNING
        or state.pending_tool is None
        or state.pending_tool.node_key != occurrence.logical_step_id
        or f"tool:{state.pending_tool.tool}/v1" != occurrence.provider
        or encode(state.pending_tool.arguments) != context.request
        or state.attempts.get(occurrence.logical_step_id) != previous.attempt
        or candidates[0].execution_policy_ref != policy.digest()
    ):
        raise AgentGitError(ErrorCode.FORBIDDEN, "新执行尝试缺少原身份、输入及结算依据")
    return candidates[0]


def validate_retry(
    connection: Connection,
    history: HistoryService,
    lease: Lease,
    runs: tuple[UUID, ...],
    previous: StoppedExecution,
    policy: ExecutionPolicy,
) -> None:
    checked = _stopped_row(connection, lease, runs, previous.occurrence, policy)
    if checked is None or checked[0]["input_manifest_ref"] != previous.input_manifest_ref:
        raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "原执行未确认停止，禁止创建新尝试")
    _cleanup_proof(history, lease, checked)


def _cleanup_proof(
    history: HistoryService, lease: Lease, checked: tuple[dict[str, Any], str]
) -> bytes:
    execution, proof_ref = checked
    raw = history.objects.get(proof_ref, ObjectType.BLOB)
    if len(raw) > 4096:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "容器清理证据超过固定大小")
    proof = parse_contract(raw, _CleanupProof)
    if (
        proof.container_name != execution["container_name"]
        or proof.container_id != execution["container_id"]
        or proof.previous_fencing_token != execution["fencing_token"]
        or not proof.previous_fencing_token < proof.observer_fencing_token <= lease.fencing_token
    ):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "旧容器清理证据与执行身份不符")
    return raw


def discard_stopped(
    session: ResearchSession,
    queue: JobService,
    lease: Lease,
    origins: tuple[UUID, ...],
    calls: list[dict[str, Any]],
    fault: Callable[[str], None],
) -> bool:
    """原子记录丢弃决定和已消耗预算；不补写或伪造旧调用结果。"""
    policy = session.config.execution_policy
    if policy is None or len(calls) != 1 or calls[0]["provider"] not in SANDBOX_PROVIDERS:
        return False
    call, history = calls[0], session.history
    occurrence = occurrence_of(call)
    runs = (lease.run_id, *origins)
    with history.engine.begin() as connection:
        run = history._run(connection, lease.repository_id, lease.run_id, lock=True)
        history.assert_writer(connection, lease.repository_id, lease.run_id)
        queue.guard(connection, lease, lease.run_id)
        if run["checkpoint_commit_id"] != session.head:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "执行结算检查点已改变")
        checked = _stopped_row(connection, lease, runs, occurrence, policy)
    if checked is None or checked[0]["call_id"] != call["id"]:
        return False
    execution, proof_ref = checked
    raw = _cleanup_proof(history, lease, checked)
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    if (
        intent.occurrence != occurrence
        or intent.checkpoint is None
        or intent.checkpoint != execution["checkpoint_commit_id"]
        or intent.request_ref != occurrence.request_digest
        or intent.effect != EffectClass.NON_IDEMPOTENT_WRITE
        or intent.max_response_bytes != session.config.limits.max_response_bytes
        or session.state.pending_tool is None
        or occurrence.signature_digest != session.tools.signature(session.state.pending_tool.tool)
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "旧执行意图与当前工具不符")
    before = parse_contract(
        encode(history.restore(lease.repository_id, intent.checkpoint).data), AgentState
    )
    decision, state = before.pending_tool, session.state
    if (
        decision is None
        or before.status != RunStatus.RUNNING
        or occurrence.logical_step_id != decision.node_key
        or occurrence.provider != f"tool:{decision.tool}/v1"
        or occurrence.attempt != before.attempts.get(decision.node_key, 0) + 1
        or history.objects.get(intent.request_ref, ObjectType.BLOB) != encode(decision.arguments)
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "旧执行与调用前状态不符")
    usage = evolve(before.usage, tool_calls=before.usage.tool_calls + 1)
    attempts = {**before.attempts, decision.node_key: occurrence.attempt}
    if state.status == RunStatus.OUTCOME_UNKNOWN:
        normalized = evolve(
            state,
            status=before.status,
            error_code=before.error_code,
            usage=before.usage,
            attempts=before.attempts,
            messages=before.messages,
            resume_status=before.resume_status,
        )
        extra = state.messages[len(before.messages) :]
        if (
            normalized != before
            or state.error_code != ErrorCode.OUTCOME_UNKNOWN.value
            or state.resume_status != before.status
            or state.usage != usage
            or state.attempts != attempts
            or state.messages[: len(before.messages)] != before.messages
            or len(extra) > 1
            or (
                extra
                and (
                    extra[0].logical_step_id != occurrence.logical_step_id
                    or extra[0].attempt != occurrence.attempt
                    or extra[0].provider != occurrence.provider
                    or extra[0].signature_digest != occurrence.signature_digest
                    or extra[0].request_ref != occurrence.request_digest
                    or extra[0].error_code != ErrorCode.OUTCOME_UNKNOWN.value
                )
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知执行预算或轨迹不符")
    elif state != before:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "执行结算不能改写调用前状态")
    context = session._execution_context()
    if context.input_manifest_ref != execution["input_manifest_ref"]:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "丢弃后不得改变原执行输入")
    stopped = StoppedExecution(
        occurrence=occurrence,
        input_manifest_ref=context.input_manifest_ref,
        execution_policy_ref=policy.digest(),
    )
    if stopped in before.stopped_executions:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "旧执行不能重复结算")
    next_state = evolve(
        before,
        usage=usage,
        attempts=attempts,
        messages=state.messages,
        stopped_executions=(*before.stopped_executions, stopped),
    )
    evidence = history.objects.put(ObjectType.BLOB, raw)

    def guard(connection: Connection) -> None:
        queue.guard(connection, lease, lease.run_id)
        current = _stopped_row(connection, lease, runs, occurrence, policy)
        if current != checked:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "旧执行或清理证据发生变化")
        history._ref(
            connection,
            lease.repository_id,
            evidence,
            "execution_discard",
            str(lease.run_id),
            occurrence.request_digest,
        )
        fault("sandbox_discard_transaction")

    session.save(
        next_state,
        "sandbox.execution_discarded",
        {
            "call_id": str(call["id"]),
            "cleanup_evidence_ref": proof_ref,
            "stopped_execution": stopped.model_dump(mode="json"),
            "charged_tool_calls": 1,
        },
        guard=guard,
    )
    fault("sandbox_after_discard")
    return True
