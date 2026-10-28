"""从明确来源路径和录制调用构造可信合并输入；不执行 Adapter 或写 CAS。"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import ReadArguments, ToolRegistry
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import SANDBOX_PROVIDERS, ExecutionResult
from agentgit.core.merge import (
    ApprovedMergeOrigin,
    MergedResearchState,
    MergeEffect,
    MergeOrigin,
    MergeReadProof,
    MergeSourceOrigin,
)
from agentgit.core.models import (
    CallIntent,
    CommitManifest,
    EffectClass,
    EnvironmentManifest,
    parse_contract,
)
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    CallTrace,
    ResearchGoal,
    SavedCallOutcome,
)
from agentgit.core.versions import RecordedRange
from agentgit.merge.state import StateMergeInput
from agentgit.replay.workspace import MemoryWorkspace
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.versions import VersionService

MAX_CALLS = 1000
MAX_INPUTS = 1000
MAX_MERGE_DEPTH = 32


@dataclass
class _InputContext:
    cache: dict[bytes, StateMergeInput] = field(default_factory=dict)
    active: set[bytes] = field(default_factory=set)
    merge_depth: int = 0


def base_origin(
    history: HistoryService,
    repository: UUID,
    source: UUID,
    commit: str,
    *,
    selected_run: UUID | None = None,
) -> MergeSourceOrigin:
    versions = VersionService(history)
    with history.engine.connect() as connection:
        family = versions._family(connection, repository, source)
        source_run = history._run(connection, repository, source)
        links = TABLES["run_commits"]
        owners = set(
            connection.execute(
                sa.select(links.c.run_id).where(
                    links.c.repository_id == repository,
                    links.c.commit_id == commit,
                    links.c.run_id.in_(family),
                )
            ).scalars()
        )
    if selected_run is not None:
        if source_run["checkpoint_commit_id"] is None or commit not in versions.ancestors(
            repository, source_run["checkpoint_commit_id"]
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "显式 base 不在来源祖先内")
        versions.select_range(
            repository,
            RecordedRange(source_run_id=str(selected_run), from_commit=commit, until_commit=commit),
        )
        return MergeOrigin(run_id=str(selected_run), commit_id=commit)
    for run in family:
        if run in owners:
            return MergeOrigin(run_id=str(run), commit_id=commit)
    if source_run["checkpoint_commit_id"] is None or commit not in versions.ancestors(
        repository, source_run["checkpoint_commit_id"]
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "共同祖先不在明确来源历史内")
    with history.engine.connect() as connection:
        approvals = TABLES["merge_approvals"]
        approval = connection.execute(
            sa.select(approvals.c.id).where(
                approvals.c.repository_id == repository,
                approvals.c.commit_id == commit,
            )
        ).scalar_one_or_none()
        if approval is not None:
            return ApprovedMergeOrigin(approval_id=str(approval), commit_id=commit)
        owners = set(
            connection.execute(
                sa.select(links.c.run_id)
                .where(
                    links.c.repository_id == repository,
                    links.c.commit_id == commit,
                )
                .limit(2)
            ).scalars()
        )
    if len(owners) != 1:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "共同祖先缺少唯一明确的 Run 或批准来源")
    return MergeOrigin(run_id=str(next(iter(owners))), commit_id=commit)


def load_state_input(
    history: HistoryService,
    repository: UUID,
    origin: MergeSourceOrigin,
    corpus: Corpus,
    *,
    _context: _InputContext | None = None,
) -> StateMergeInput:
    context = _context or _InputContext()
    key = encode(dict(repository_id=str(repository), origin=origin.model_dump(mode="json")))
    if key in context.cache:
        return context.cache[key]
    if key in context.active:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并来源包含循环")
    if len(context.cache) + len(context.active) >= MAX_INPUTS:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并来源超过有界数量")
    approved = isinstance(origin, ApprovedMergeOrigin)
    if approved and context.merge_depth >= MAX_MERGE_DEPTH:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "批准来源超过有界深度")
    context.active.add(key)
    context.merge_depth += int(approved)
    try:
        result = (
            _load_approved_input(history, repository, origin, corpus, context)
            if isinstance(origin, ApprovedMergeOrigin)
            else _load_run_input(history, repository, origin, corpus)
        )
        context.cache[key] = result
        return result
    finally:
        context.active.remove(key)
        context.merge_depth -= int(approved)


def load_state_inputs(
    history: HistoryService,
    repository: UUID,
    origins: tuple[MergeSourceOrigin, ...],
    corpus: Corpus,
) -> tuple[StateMergeInput, ...]:
    context = _InputContext()
    return tuple(
        load_state_input(history, repository, origin, corpus, _context=context)
        for origin in origins
    )


def _load_approved_input(
    history: HistoryService,
    repository: UUID,
    origin: ApprovedMergeOrigin,
    corpus: Corpus,
    context: _InputContext,
) -> StateMergeInput:
    # 延迟导入，避免提案编译器与已批准快照读取器在加载阶段互相依赖。
    from agentgit.services.merge_runs import approved_snapshot

    with history.engine.connect() as connection:
        snapshot = approved_snapshot(
            history, connection, repository, UUID(origin.approval_id), origin.commit_id
        )
    state = parse_contract(encode(snapshot.data), MergedResearchState)
    if state.constraints.evaluation_time != corpus.evaluation_time:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "合并基点的评估时间不符")
    prior = tuple(
        load_state_input(history, repository, source.origin, corpus, _context=context)
        for source in state.source_usage
    )
    ancestors = set(VersionService(history).ancestors(repository, origin.commit_id))
    if any(item.origin.commit_id not in ancestors for item in prior):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "批准来源不在合并基点祖先内")
    proofs = {encode(proof.model_dump(mode="json")) for item in prior for proof in item.read_proofs}
    if any(encode(proof.model_dump(mode="json")) not in proofs for proof in state.read_proofs):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并基点的读取证明缺少原始来源")
    effects = {effect.call_id: effect for item in prior for effect in item.effects}
    if any(effects.get(effect.call_id) != effect for effect in state.effects_audit):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并基点的效果审计与实际来源不同")
    manifest = parse_contract(
        history.objects.get(origin.commit_id, ObjectType.COMMIT), CommitManifest
    )
    # 只用于比较；用量与完成均不声明为此合并 Commit 的执行事实。
    projection = AgentState(goal=state.goal, plan=state.plan, working_memory=state.working_memory)
    return StateMergeInput(
        origin,
        manifest.state_ref,
        projection,
        state.constraints,
        state.read_proofs,
        tuple(effects[key] for key in sorted(effects)),
    )


def _load_run_input(
    history: HistoryService, repository: UUID, origin: MergeOrigin, corpus: Corpus
) -> StateMergeInput:
    versions = VersionService(history)
    versions.select_range(
        repository,
        RecordedRange(
            source_run_id=origin.run_id, from_commit=origin.commit_id, until_commit=origin.commit_id
        ),
    )
    snapshot = history.restore(repository, origin.commit_id)
    manifest = parse_contract(
        history.objects.get(origin.commit_id, ObjectType.COMMIT), CommitManifest
    )
    environment = parse_contract(
        history.objects.get(manifest.environment_ref, ObjectType.ENVIRONMENT), EnvironmentManifest
    )
    controlled = decode(history.objects.get(environment.input_ref, ObjectType.BLOB))
    if (
        not isinstance(controlled, dict)
        or type(controlled.get("schema_version")) is not int
        or controlled["schema_version"] != 1
        or set(controlled)
        - {
            "schema_version",
            "goal",
            "config",
            "corpus_digest",
            "budget_baseline",
            "hybrid_policy_ref",
        }
        or not {"goal", "config", "corpus_digest"} <= set(controlled)
    ):
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "合并来源没有受支持的冻结研究输入")
    state = parse_contract(encode(snapshot.data), AgentState)
    config = parse_contract(encode(controlled["config"]), AgentConfig)
    goal = parse_contract(encode(controlled["goal"]), ResearchGoal)
    if (
        state.goal != goal
        or controlled["corpus_digest"] != corpus.digest()
        or environment.evaluation_time != corpus.evaluation_time
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "合并来源任务、语料或时钟与冻结验证输入不同")
    ancestors = set(versions.ancestors(repository, origin.commit_id))
    with history.engine.connect() as connection:
        family = versions._family(connection, repository, UUID(origin.run_id))
        table = TABLES["external_calls"]
        rows = list(
            connection.execute(
                sa.select(table)
                .where(table.c.repository_id == repository, table.c.run_id.in_(family))
                .order_by(
                    table.c.run_id,
                    table.c.logical_step_id,
                    table.c.call_index,
                    table.c.attempt,
                    table.c.id,
                )
                .limit(MAX_CALLS + 1)
            ).mappings()
        )
    if len(rows) > MAX_CALLS:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "来源调用数量超过合并核验上限")
    traces = {encode(trace.model_dump(mode="json")) for trace in state.messages}
    # 这里只核对已登记的读取 Schema，不向新配置授予调用权限。
    registry = ToolRegistry(corpus, MemoryWorkspace(max_file_bytes=1, max_files=1), AgentConfig())
    reads: list[MergeReadProof] = []
    effects: list[MergeEffect] = []
    for row in rows:
        intent = parse_contract(history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent)
        if (
            intent.effect.value != row["effect"]
            or intent.request_ref != row["request_digest"]
            or any(
                getattr(intent.occurrence, key) != row[key]
                for key in (
                    "logical_step_id",
                    "call_index",
                    "attempt",
                    "provider",
                    "signature_digest",
                    "request_digest",
                )
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并来源调用索引与意图不符")
        saved = None
        trace = None
        response = None
        selected = False
        if row["result_ref"] is not None:
            saved = parse_contract(
                history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
            )
            response = history.objects.get(saved.response_ref, ObjectType.BLOB)
            if saved.response_bytes < len(response) or (
                saved.complete and saved.response_bytes != len(response)
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "合并来源响应长度不符")
            trace = CallTrace(
                logical_step_id=intent.occurrence.logical_step_id,
                call_index=intent.occurrence.call_index,
                attempt=intent.occurrence.attempt,
                provider=intent.occurrence.provider,
                signature_digest=intent.occurrence.signature_digest,
                request_ref=intent.request_ref,
                response_ref=saved.response_ref,
                error_code=saved.error["code"] if saved.error else None,
            )
            # 相同 occurrence/请求/响应仍可能来自分叉后的其他轨迹；必须同时属于选定祖先。
            selected = (
                encode(trace.model_dump(mode="json")) in traces and intent.checkpoint in ancestors
            )
        pending = (
            row["status"] in ("proposed", "started", "outcome_unknown") or row["result_ref"] is None
        )
        # 未决调用尚未进入 messages；仍需依据意图前检查点检查，不能被状态投影隐藏。
        if not selected and not (
            pending and (intent.checkpoint is None or intent.checkpoint in ancestors)
        ):
            continue
        cleanup = False
        if selected and row["provider"] in SANDBOX_PROVIDERS and response is not None:
            result = parse_contract(response, ExecutionResult)
            with history.engine.connect() as connection:
                executions = TABLES["sandbox_executions"]
                managed = connection.execute(
                    sa.select(executions.c.cleanup_confirmed).where(
                        executions.c.repository_id == repository,
                        executions.c.call_id == row["id"],
                        executions.c.run_id == row["run_id"],
                        executions.c.policy_ref == result.execution_policy_ref,
                        executions.c.input_manifest_ref == result.input_manifest_ref,
                    )
                ).scalar_one_or_none()
            cleanup = (
                managed is True
                and result.cleanup_confirmed
                and config.execution_policy is not None
                and config.execution_policy.digest() == result.execution_policy_ref
            )
        if row["effect"] != EffectClass.READ_ONLY.value or pending:
            effects.append(
                MergeEffect(
                    call_id=str(row["id"]),
                    run_id=str(row["run_id"]),
                    provider=row["provider"],
                    effect=EffectClass(row["effect"]),
                    status=row["status"],
                    intent_ref=row["intent_ref"],
                    result_ref=row["result_ref"],
                    cleanup_confirmed=cleanup,
                )
            )
        if (
            not selected
            or row["status"] != "succeeded"
            or row["effect"] != EffectClass.READ_ONLY.value
            or row["provider"] != "tool:source_read/v1"
            or saved is None
            or not saved.complete
            or saved.error is not None
            or trace is None
            or response is None
        ):
            continue
        request = parse_contract(
            history.objects.get(intent.request_ref, ObjectType.BLOB), ReadArguments
        )
        if trace.signature_digest != registry.signature("source_read"):
            continue
        raw: Any = decode(response)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "source"}
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
        ):
            continue
        evidence = state.working_memory.get(request.source_id)
        if (
            evidence is not None
            and raw["source"] == evidence.source.model_dump(mode="json")
            and (evidence.logical_step_id, evidence.call_index, evidence.attempt)
            == (trace.logical_step_id, trace.call_index, trace.attempt)
        ):
            reads.append(
                MergeReadProof(
                    origin=origin,
                    call_id=str(row["id"]),
                    call_run_id=str(row["run_id"]),
                    evidence=evidence,
                    trace=trace,
                )
            )
    return StateMergeInput(origin, manifest.state_ref, state, config, tuple(reads), tuple(effects))
