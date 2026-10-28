"""已核验输入的状态合并向量；不冒充真实来源读取或持久审批。"""

from dataclasses import replace
from uuid import UUID

import pytest

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import research_plan
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.merge import ConflictKind, MergeEffect, MergeOrigin, MergeReadProof
from agentgit.core.models import EffectClass, RunStatus, parse_contract
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    BudgetUsage,
    CallTrace,
    Evidence,
    Plan,
    PlanNode,
    ResearchGoal,
    ToolDecision,
    evolve,
)
from agentgit.merge.state import StateMergeInput, merge_state


def incoming(number, *, memory=None, plan=None, completed=(), config=None, usage=None):
    origin = MergeOrigin(run_id=str(UUID(int=number)), commit_id=f"{number:064x}")
    evidence = (
        {
            key: Evidence(source=source, logical_step_id="read-" + key, attempt=1)
            for key, source in Corpus.bundled().sources.items()
        }
        if memory is None
        else memory
    )
    state = AgentState(
        goal=ResearchGoal(text="比较三个框架"),
        plan=plan or research_plan(),
        working_memory=evidence,
        completed=completed,
        usage=usage or BudgetUsage(),
    )
    proofs = tuple(
        MergeReadProof(
            origin=origin,
            call_id=str(UUID(int=number * 100 + index)),
            call_run_id=origin.run_id,
            evidence=item,
            trace=CallTrace(
                logical_step_id=item.logical_step_id,
                call_index=item.call_index,
                attempt=item.attempt,
                provider="tool:source_read/v1",
                signature_digest="a" * 64,
                request_ref="b" * 64,
                response_ref="c" * 64,
            ),
        )
        for index, item in enumerate(evidence.values())
    )
    return StateMergeInput(origin, f"{number:064x}", state, config or AgentConfig(), proofs)


def merge(*items):
    return merge_state(*items, Corpus.bundled())


def kinds(candidate):
    assert candidate.proposed_state is None
    return {item.kind for item in candidate.conflicts}


def test_disjoint_evidence_merges_without_call_trajectory_or_budget_fabrication():
    normal = incoming(1)
    keys = sorted(normal.state.working_memory)
    left = incoming(
        2,
        memory={key: normal.state.working_memory[key] for key in keys[:3]},
        usage=BudgetUsage(model_calls=4),
    )
    right = incoming(
        3,
        memory={key: normal.state.working_memory[key] for key in keys[3:]},
        usage=BudgetUsage(model_calls=7),
    )
    candidate = merge(incoming(1, memory={}), left, right)
    state = candidate.proposed_state
    assert state is not None and not candidate.conflicts and len(state.working_memory) == 6
    assert [item.usage.model_calls for item in state.source_usage] == [0, 4, 7]
    assert state.execution == "requires_new_run" and "messages" not in state.model_dump()
    with pytest.raises(AgentGitError):
        parse_contract(encode(state.model_dump(mode="json")), AgentState)


def test_equal_source_with_different_valid_occurrence_keeps_complete_proofs():
    original = incoming(1)
    memory = {
        key: evolve(item, logical_step_id="other-" + item.source.id)
        for key, item in original.state.working_memory.items()
    }
    left = incoming(2, memory=memory)
    result = merge(original, left, incoming(3))
    assert result.proposed_state is not None
    assert result.proposed_state.working_memory == memory
    assert len(result.proposed_state.read_proofs) == 18


def test_unproven_memory_does_not_become_trusted_because_it_has_a_source_id():
    left = replace(incoming(2), read_proofs=())
    left = replace(
        left,
        state=evolve(
            left.state,
            working_memory={
                key: evolve(item, logical_step_id="unrecorded")
                for key, item in left.state.working_memory.items()
            },
        ),
    )
    assert ConflictKind.PROVENANCE_MISSING in kinds(merge(incoming(1), left, incoming(3)))


def test_delete_vs_edit_same_source_is_a_fact_conflict():
    normal = incoming(1)
    key = next(iter(normal.state.working_memory))
    item = normal.state.working_memory[key]
    edited = evolve(item, source=evolve(item.source, url="fixture://different"))
    assert ConflictKind.FACT_CONTRADICTION in kinds(
        merge(
            incoming(1, memory={key: item}),
            incoming(2, memory={}),
            incoming(3, memory={key: edited}),
        )
    )


def test_different_valid_single_branch_dags_can_form_a_cycle_when_combined():
    a = PlanNode(key="a", kind="search")
    b = PlanNode(key="b", kind="write_report")
    base = Plan(nodes=(a, b))
    left = Plan(nodes=(evolve(a, dependencies=("b",)), b))
    right = Plan(nodes=(a, evolve(b, dependencies=("a",))))
    assert ConflictKind.PLAN_DEPENDENCY in kinds(
        merge(incoming(1, plan=base), incoming(2, plan=left), incoming(3, plan=right))
    )


def test_completed_node_rewrite_or_missing_predecessor_is_blocked():
    normal = incoming(1, completed=("search",))
    plan = evolve(
        normal.state.plan,
        nodes=(
            PlanNode(key="prepare", kind="write_report"),
            *tuple(
                evolve(node, dependencies=("prepare",)) if node.key == "search" else node
                for node in normal.state.plan.nodes
            ),
        ),
    )
    assert ConflictKind.PLAN_DEPENDENCY in kinds(
        merge(
            normal,
            incoming(2, plan=plan, completed=("search",)),
            incoming(3, completed=("search",)),
        )
    )
    assert ConflictKind.PLAN_DEPENDENCY in kinds(
        merge(incoming(1), incoming(2, completed=("read-0",)), incoming(3))
    )


@pytest.mark.parametrize(
    "limits", [dict(max_model_calls=1), dict(max_tool_calls=1), dict(max_steps=1)]
)
def test_minimum_complete_plan_work_must_fit_every_original_ceiling(limits):
    cfg = evolve(AgentConfig(), limits=evolve(AgentConfig().limits, **limits))
    assert ConflictKind.CONSTRAINT_VIOLATION in kinds(
        merge(incoming(1), incoming(2, config=cfg), incoming(3))
    )


def test_permissions_are_intersected_and_finite_cost_ceiling_is_preserved():
    cfg = evolve(
        AgentConfig(), limits=evolve(AgentConfig().limits, max_cost_micro_usd=0, max_wall_ms=1000)
    )
    result = merge(incoming(1), incoming(2, config=cfg), incoming(3))
    assert result.proposed_state.constraints.limits.max_cost_micro_usd == 0
    assert result.proposed_state.constraints.limits.max_wall_ms == 1000
    assert ConflictKind.CONSTRAINT_VIOLATION in kinds(
        merge(incoming(1), incoming(2, config=AgentConfig(tools=("artifact_write",))), incoming(3))
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(status=RunStatus.OUTCOME_UNKNOWN),
        dict(status=RunStatus.WAITING_APPROVAL),
        dict(
            pending_tool=ToolDecision(node_key="write-report", tool="artifact_write", arguments={})
        ),
    ],
)
def test_pending_state_is_never_silently_removed(change):
    left = incoming(2)
    left = replace(left, state=evolve(left.state, **change))
    assert ConflictKind.EFFECT_CONFLICT in kinds(merge(incoming(1), left, incoming(3)))


def test_reserved_budget_is_preserved_as_a_blocker():
    assert ConflictKind.CONSTRAINT_VIOLATION in kinds(
        merge(incoming(1), incoming(2, usage=BudgetUsage(reserved_tokens=1)), incoming(3))
    )


def effect(**changes):
    value = MergeEffect(
        call_id=str(UUID(int=100)),
        run_id=str(UUID(int=2)),
        provider="fixture:payment",
        effect=EffectClass.IRREVERSIBLE,
        status="succeeded",
        intent_ref="d" * 64,
        result_ref="e" * 64,
    )
    return evolve(value, **changes)


@pytest.mark.parametrize(
    "record",
    [
        effect(),
        effect(status="outcome_unknown", result_ref=None),
        effect(effect=EffectClass.NON_IDEMPOTENT_WRITE),
        effect(provider="tool:code_exec/v1", cleanup_confirmed=False),
    ],
)
def test_new_external_effect_or_unsettled_sandbox_blocks_without_invocation(record):
    left = replace(incoming(2), effects=(record,))
    assert ConflictKind.EFFECT_CONFLICT in kinds(merge(incoming(1), left, incoming(3)))


def test_identical_settled_common_effect_is_an_audit_record_only():
    record = effect()
    result = merge(*(replace(incoming(index), effects=(record,)) for index in (1, 2, 3)))
    assert result.proposed_state is not None and result.proposed_state.effects_audit == (record,)
    assert result.proposed_state.execution == "requires_new_run"


def test_expired_source_and_unknown_dimension_are_not_accepted_as_new_facts():
    normal = incoming(1)
    key = next(iter(normal.state.working_memory))
    item = normal.state.working_memory[key]
    expired = evolve(item, source=evolve(item.source, valid_until="2020-01-01T00:00:00Z"))
    assert ConflictKind.TEMPORAL_MISMATCH in kinds(
        merge(
            normal, incoming(2, memory={**normal.state.working_memory, key: expired}), incoming(3)
        )
    )
    strange = evolve(
        item,
        source=evolve(item.source, facts=(evolve(item.source.facts[0], dimension="unknown-unit"),)),
    )
    assert ConflictKind.CONSTRAINT_VIOLATION in kinds(
        merge(
            normal, incoming(2, memory={**normal.state.working_memory, key: strange}), incoming(3)
        )
    )
