"""方法选择的纯契约边界，不当作 30 例或任何真实修复成功率。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from repair_benchmark_protocol import (
    GLOBAL_PROMPT_TEMPLATE,
    METHODS,
    common_budget_ref,
    method_plan,
)

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError
from agentgit.core.interventions import InterventionPrecondition
from agentgit.core.models import CallOccurrence
from agentgit.core.repair import RepairBinding
from agentgit.core.research import AgentConfig, AgentState, Plan, PlanNode, ResearchGoal, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.policy import mock_budget, mock_policy_version
from agentgit.repair.space import enumerate_proposals, freeze_space, patch_ref
from agentgit.repair.templates import TemplateAnchor, model_templates


@pytest.fixture
def inputs():
    config = AgentConfig()
    snapshot = StateSnapshot(
        data=AgentState(
            goal=ResearchGoal(text="方法选择纯向量"),
            plan=Plan(nodes=(PlanNode(key="report", kind="write_report"),)),
        ).model_dump(mode="json")
    )
    anchor = TemplateAnchor(
        source_run_id=str(uuid4()),
        base_commit="a" * 64,
        target_commit_id="a" * 64,
        target_event_id=7,
        precondition=InterventionPrecondition(
            state_ref=object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json"))),
            config_ref=object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json"))),
            validator_ref="d" * 64,
            occurrence=CallOccurrence(
                logical_step_id="model-1",
                call_index=0,
                attempt=1,
                provider="llm:mock-research/v1",
                signature_digest="e" * 64,
                request_digest="f" * 64,
            ),
        ),
    )
    budget = mock_budget(config)
    binding = RepairBinding(
        repository_id=str(uuid4()),
        failed_run_id=anchor.source_run_id,
        base_commit=anchor.base_commit,
        environment_ref="b" * 64,
        validator_ref="d" * 64,
        policy_version=mock_policy_version(budget),
    )

    def no_blob(ref):
        pytest.fail("纯向量不应读取外部产物")

    batch = model_templates(anchor, config, snapshot, no_blob)
    space = freeze_space(binding, reversed(batch.patches), source_reason="纯契约向量")
    return space, budget, tuple(str(uuid4()) for _ in space.patches)


@pytest.mark.parametrize("method", METHODS)
def test_all_methods_keep_common_origin_limits_and_exact_intervention_mapping(inputs, method):
    space, budget, ids = inputs
    planned = method_plan(method, space, budget, ids, first_exception_target=7)
    assert common_budget_ref(planned.budget) == common_budget_ref(budget)
    assert planned.budget.trial == budget.trial
    assert planned.space.binding.model_dump(exclude={"policy_version"}) == space.binding.model_dump(
        exclude={"policy_version"}
    )
    source = {patch_ref(p): identity for p, identity in zip(space.patches, ids, strict=True)}
    assert [source[patch_ref(p)] for p in planned.space.patches] == list(planned.intervention_ids)
    assert (
        planned.identity()
        == method_plan(method, space, budget, ids, first_exception_target=7).identity()
    )


def test_fallback_keeps_production_order_and_is_honestly_identical_to_unordered(inputs):
    space, budget, ids = inputs
    full = method_plan("repair", space, budget, ids, first_exception_target=None)
    unordered = method_plan(
        "unordered_enumeration", space, budget, ids, first_exception_target=None
    )
    assert full.space.patches == unordered.space.patches
    assert full.space.patches == space.patches
    assert full.intervention_ids == unordered.intervention_ids
    assert full.identity() != unordered.identity()
    ranked = evolve(space, candidate_source="static_ranking")
    with pytest.raises(AgentGitError):
        method_plan("repair", ranked, budget, ids, first_exception_target=None)


def test_global_prompt_is_one_frozen_proposal_and_missing_local_target_stays_empty(inputs):
    space, budget, ids = inputs
    global_plan = method_plan("global_prompt_once", space, budget, ids, first_exception_target=None)
    assert [p.template for p in global_plan.space.patches] == [GLOBAL_PROMPT_TEMPLATE]
    assert len(tuple(enumerate_proposals(global_plan.space))) == 1
    local = method_plan("first_exception_local", space, budget, ids, first_exception_target=999)
    assert local.space.patches == () and local.intervention_ids == ()
    assert set(local.space.excluded_patch_refs) == {patch_ref(p) for p in space.patches}
    assert space.patches


def test_no_reduction_redistributes_only_six_slots_and_never_steals_confirm_regression(inputs):
    space, budget, ids = inputs
    planned = method_plan(
        "combination_without_reduction", space, budget, ids, first_exception_target=None
    )
    assert planned.budget.quotas.model_dump(exclude={"schema_version"}) == dict(
        screen=20, reduce=0, confirm=6, regression=4
    )
    assert planned.max_reduce_trials == 0 and planned.field_policy is None
    assert planned.budget.model_dump(exclude={"quotas"}) == budget.model_dump(exclude={"quotas"})
    assert planned.space.binding.policy_version == mock_policy_version(planned.budget)
    assert space.binding.policy_version == mock_policy_version(budget)


@pytest.mark.parametrize("target", [True, 0, -1, "7"])
def test_invalid_event_cannot_silently_select_a_different_local_target(inputs, target):
    with pytest.raises(AgentGitError):
        method_plan("first_exception_local", *inputs, first_exception_target=target)


@pytest.mark.parametrize(
    "change",
    [
        "provider",
        "mismatched_budget",
        "changed_stage_profile",
        "missing_id",
        "duplicate_id",
        "invalid_id",
        "unknown_method",
    ],
)
def test_method_boundary_rejects_unfrozen_budget_provider_or_invalid_mapping(inputs, change):
    space, budget, ids = inputs
    method = "repair"
    if change == "provider":
        space = evolve(space, binding=evolve(space.binding, policy_version="bounded-templates/v1"))
    elif change == "mismatched_budget":
        budget = evolve(budget, max_wall_ms=budget.max_wall_ms - 1)
    elif change == "changed_stage_profile":
        budget = evolve(budget, quotas=evolve(budget.quotas, screen=13))
        space = evolve(
            space, binding=evolve(space.binding, policy_version=mock_policy_version(budget))
        )
    elif change == "missing_id":
        ids = ids[:-1]
    elif change == "duplicate_id":
        ids = (ids[0],) * len(ids)
    elif change == "invalid_id":
        ids = ("../file", *ids[1:])
    else:
        method = "choose_after_seeing_confirmation"
    with pytest.raises((AgentGitError, ValidationError)):
        method_plan(method, space, budget, ids, first_exception_target=7)
