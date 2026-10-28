"""研究状态的有限三方投影；不会生成可直接恢复执行的 AgentState。"""

from dataclasses import dataclass

from pydantic import ValidationError

from agentgit.adapters.corpus import Corpus, utc_time
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import SANDBOX_PROVIDERS
from agentgit.core.merge import (
    ConflictKind,
    MergeCompletedClaim,
    MergeConflict,
    MergeConstraints,
    MergedResearchState,
    MergeEffect,
    MergeOrigin,
    MergeReadProof,
    MergeSourceUsage,
)
from agentgit.core.models import EffectClass, RunStatus
from agentgit.core.research import AgentConfig, AgentState, BudgetLimits, Evidence, Plan
from agentgit.diff.structural import pointer_part
from agentgit.runtime.validation import DIMENSIONS

_ABSENT = object()
_CLASH = object()
TOOLS = {
    "search": "corpus_search",
    "read": "source_read",
    "write_report": "artifact_write",
    "write_sources": "artifact_write",
    "write_execution_code": "artifact_write",
    "write_execution_command": "artifact_write",
    "code_exec": "code_exec",
    "terminal_exec": "terminal_exec",
}


@dataclass(frozen=True)
class StateMergeInput:
    """只能由可信读取器建立；纯内核不把调用方声明变成持久证明。"""

    origin: MergeOrigin
    state_ref: str
    state: AgentState
    config: AgentConfig
    read_proofs: tuple[MergeReadProof, ...] = ()
    effects: tuple[MergeEffect, ...] = ()


@dataclass(frozen=True)
class StateCandidate:
    proposed_state: MergedResearchState | None
    conflicts: tuple[MergeConflict, ...]


def merge_state(
    base: StateMergeInput, ours: StateMergeInput, theirs: StateMergeInput, corpus: Corpus
) -> StateCandidate:
    inputs = (base, ours, theirs)
    refs = tuple(dict.fromkeys(item.state_ref for item in inputs))
    conflicts: list[MergeConflict] = []

    def conflict(path: str, kind: ConflictKind) -> None:
        item = MergeConflict(kind=kind, paths=(path,), evidence_refs=refs)
        if item not in conflicts:
            if len(conflicts) >= 1000:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "状态合并冲突超过上限")
            conflicts.append(item)

    def same(left: object, right: object) -> bool:
        if left is _ABSENT or right is _ABSENT:
            return left is right
        return encode(left) == encode(right)

    def select(values: tuple[object, ...]) -> object:
        before, left, right = values
        if same(left, right):
            return left
        if same(left, before):
            return right
        if same(right, before):
            return left
        return _CLASH

    if any(item.state.goal != base.state.goal for item in inputs):
        conflict("/state/goal", ConflictKind.CONSTRAINT_VIOLATION)
    for item in inputs:
        state = item.state
        if (
            len(state.working_memory) > 1000
            or len(item.effects) > 1000
            or len(item.read_proofs) > 1000
            or len(state.completed) > 64
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "输入状态实体超过上限")
        if (
            state.status
            in (RunStatus.OUTCOME_UNKNOWN, RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED)
            or state.pending_tool is not None
            or state.stopped_executions
        ):
            conflict("/state/pending", ConflictKind.EFFECT_CONFLICT)
        if state.usage.reserved_tokens or (
            state.usage.model_meter and state.usage.model_meter.reserved_cost_micro_usd
        ):
            conflict("/state/reservations", ConflictKind.CONSTRAINT_VIOLATION)

    tables = [
        {node.key: node for node in item.state.plan.nodes} if item.state.plan else {}
        for item in inputs
    ]
    merged_nodes = {}
    for key in sorted(set().union(*(set(table) for table in tables))):
        value = select(
            tuple(
                table[key].model_dump(mode="json") if key in table else _ABSENT for table in tables
            )
        )
        if value is _CLASH:
            conflict("/state/plan/" + pointer_part(key), ConflictKind.WRITE_WRITE)
        elif value is not _ABSENT:
            merged_nodes[key] = value
    plan = None
    if merged_nodes:
        try:
            plan = Plan.model_validate_json(
                encode(dict(schema_version=1, nodes=list(merged_nodes.values())))
            )
        except ValidationError:
            conflict("/state/plan", ConflictKind.PLAN_DEPENDENCY)
    completed = []
    for item, table in zip(inputs, tables, strict=True):
        done = set(item.state.completed)
        if len(done) != len(item.state.completed):
            conflict("/state/completed", ConflictKind.PLAN_DEPENDENCY)
        for key in item.state.completed:
            if (
                key not in table
                or not set(table[key].dependencies) <= done
                or merged_nodes.get(key) != table[key].model_dump(mode="json")
            ):
                conflict("/state/completed/" + pointer_part(key), ConflictKind.PLAN_DEPENDENCY)
            else:
                completed.append(MergeCompletedClaim(origin=item.origin, node=table[key]))

    memory: dict[str, Evidence] = {}
    all_proofs = tuple(proof for item in inputs for proof in item.read_proofs)
    memory_keys = set().union(*(set(item.state.working_memory) for item in inputs))
    if len(memory_keys) > 1000:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并工作记忆超过上限")
    for key in sorted(memory_keys):
        evidence_options = [item.state.working_memory.get(key) for item in inputs]
        value = select(
            tuple(
                item.source.model_dump(mode="json") if item is not None else _ABSENT
                for item in evidence_options
            )
        )
        if value is _CLASH:
            conflict("/state/working_memory/" + pointer_part(key), ConflictKind.FACT_CONTRADICTION)
        elif value is not _ABSENT:
            # 相同来源可以有多条真实读取证明；选取一条完整 Evidence，不拼接 occurrence。
            chosen = next(
                item
                for item in (evidence_options[1], evidence_options[2], evidence_options[0])
                if item and item.source.model_dump(mode="json") == value
            )
            memory[key] = chosen
            if not any(
                proof.evidence == chosen
                and proof.trace.provider == "tool:source_read/v1"
                and proof.trace.error_code is None
                for proof in all_proofs
            ):
                conflict(
                    "/state/working_memory/" + pointer_part(key), ConflictKind.PROVENANCE_MISSING
                )
    facts: dict[tuple[str, str], str] = {}
    now = utc_time(corpus.evaluation_time)
    for key, evidence in memory.items():
        source = evidence.source
        location = "/state/working_memory/" + pointer_part(key)
        if source.id != key or key not in corpus.sources or source != corpus.sources[key]:
            conflict(location, ConflictKind.PROVENANCE_MISSING)
        try:
            valid = (
                utc_time(source.valid_from) <= now < utc_time(source.valid_until)
                and utc_time(source.collected_at) <= now
            )
        except AgentGitError:
            valid = False
        if not valid:
            conflict(location, ConflictKind.TEMPORAL_MISMATCH)
        for fact in source.facts:
            identity = (source.framework, fact.dimension)
            if fact.dimension not in DIMENSIONS:
                conflict(location, ConflictKind.CONSTRAINT_VIOLATION)
            if identity in facts and facts[identity] != fact.value:
                conflict(location, ConflictKind.FACT_CONTRADICTION)
            facts[identity] = fact.value

    caps = {}
    for name in BudgetLimits.model_fields:
        if name != "schema_version":
            values = [getattr(item.config.limits, name) for item in inputs]
            finite = [value for value in values if value is not None]
            caps[name] = min(finite) if finite else None
    limits = BudgetLimits.model_validate(dict(schema_version=1, **caps))
    tools = tuple(sorted(set.intersection(*(set(item.config.tools) for item in inputs))))
    policy = base.config.execution_policy
    if any(item.config.execution_policy != policy for item in inputs):
        conflict("/state/constraints/execution_policy", ConflictKind.CONSTRAINT_VIOLATION)
    if plan is not None:
        required = {TOOLS[node.kind] for node in plan.nodes}
        if required - set(tools):
            conflict("/state/constraints/tools", ConflictKind.CONSTRAINT_VIOLATION)
        if len(plan.nodes) > limits.max_tool_calls or len(plan.nodes) + 1 > min(
            limits.max_steps, limits.max_model_calls
        ):
            conflict("/state/constraints/limits", ConflictKind.CONSTRAINT_VIOLATION)

    base_effects = {item.call_id for item in base.effects}
    effects: dict[str, MergeEffect] = {}
    for source_input in inputs:
        for effect in source_input.effects:
            if effect.call_id in effects and effects[effect.call_id] != effect:
                conflict("/state/effects/" + effect.call_id, ConflictKind.EFFECT_CONFLICT)
            effects[effect.call_id] = effect
            pending = (
                effect.status in ("proposed", "started", "outcome_unknown")
                or effect.result_ref is None
            )
            external_write = effect.effect != EffectClass.READ_ONLY and effect.provider not in {
                "tool:artifact_write/v1",
                *SANDBOX_PROVIDERS,
            }
            sandbox_unverified = effect.provider in SANDBOX_PROVIDERS and (
                not effect.cleanup_confirmed or source_input.config.execution_policy is None
            )
            if (
                pending
                or sandbox_unverified
                or (external_write and effect.call_id not in base_effects)
            ):
                conflict("/state/effects/" + effect.call_id, ConflictKind.EFFECT_CONFLICT)
    if conflicts:
        return StateCandidate(None, tuple(conflicts))
    proof_index = {encode(proof.model_dump(mode="json")): proof for proof in all_proofs}
    projected = MergedResearchState(
        goal=base.state.goal,
        plan=plan,
        working_memory=memory,
        constraints=MergeConstraints(
            tools=tools,
            limits=limits,
            execution_policy=policy,
            evaluation_time=corpus.evaluation_time,
        ),
        completed_audit=tuple(completed),
        read_proofs=tuple(proof_index.values()),
        effects_audit=tuple(effects[key] for key in sorted(effects)),
        source_usage=tuple(
            MergeSourceUsage(origin=item.origin, usage=item.state.usage) for item in inputs
        ),
    )
    return StateCandidate(projected, ())
