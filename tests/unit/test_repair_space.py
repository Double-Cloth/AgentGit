"""固定模板、有序组合及身份边界；不将纯枚举当作修复执行证据。"""

from itertools import repeat
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.interventions import InterventionPrecondition, PlanPatch, PromptPatch
from agentgit.core.models import CallOccurrence, RunStatus
from agentgit.core.repair import RepairBinding, RepairPatch, RepairSpace
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    Plan,
    PlanNode,
    ResearchGoal,
    ToolDecision,
    evolve,
)
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.space import (
    enumerate_proposals,
    freeze_space,
    patch_ref,
    proposal_key,
    trial_key,
    validate_patches,
)
from agentgit.repair.templates import (
    PROMPT_RULES,
    TemplateAnchor,
    model_templates,
    observation_templates,
)


@pytest.fixture
def inputs():
    config = AgentConfig()
    state = AgentState(
        goal=ResearchGoal(text="比较三个合成框架"),
        status=RunStatus.RUNNING,
        plan=Plan(
            nodes=(
                PlanNode(key="search", kind="search"),
                PlanNode(key="read", kind="read", dependencies=("search",), source_slot=0),
                PlanNode(key="report", kind="write_report", dependencies=("read",)),
            )
        ),
        completed=("search", "read"),
    )
    snapshot = StateSnapshot(data=state.model_dump(mode="json"))
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
                logical_step_id="model-3",
                call_index=0,
                attempt=1,
                provider="llm:mock-research/v1",
                signature_digest="e" * 64,
                request_digest="f" * 64,
            ),
        ),
    )
    binding = RepairBinding(
        repository_id=str(uuid4()),
        failed_run_id=anchor.source_run_id,
        base_commit=anchor.base_commit,
        validator_ref=anchor.precondition.validator_ref,
        environment_ref="b" * 64,
    )
    return anchor, config, snapshot, binding


def no_blob(*args):
    pytest.fail("无产物快照不应读取外部内容")


def batch(inputs):
    anchor, config, snapshot, _ = inputs
    return model_templates(anchor, config, snapshot, no_blob)


def test_fixed_catalog_adds_only_missing_node_and_leaves_completed_nodes(inputs):
    anchor, config, snapshot, _ = inputs
    before = encode(snapshot.model_dump(mode="json"))
    generated = batch(inputs)
    assert [patch.template for patch in generated.patches] == [
        "prompt-verified-sources/v1",
        "prompt-required-artifacts/v1",
        "plan-required-artifacts/v1",
    ]
    assert generated == batch(inputs)
    plan = generated.patches[2].intervention.payload.plan
    assert plan.nodes[:-1] == Plan.model_validate_json(encode(snapshot.data["plan"])).nodes
    assert plan.nodes[-1].kind == "write_sources"
    assert plan.nodes[-1].dependencies == ("report",)
    assert encode(snapshot.model_dump(mode="json")) == before
    for patch in generated.patches:
        assert patch.intervention.precondition == anchor.precondition
        assert patch.intervention.effect_policy == "sandbox_or_approval"
        assert patch.intervention.provenance.rationale.startswith("固定模板 ")
        if isinstance(patch.intervention.payload, PromptPatch):
            assert patch.intervention.payload.prompt.startswith(config.prompt + "\n")


@pytest.mark.parametrize("field", ["state_ref", "config_ref"])
def test_templates_reject_stale_inputs(inputs, field):
    anchor, config, snapshot, _ = inputs
    bad = evolve(anchor, precondition=evolve(anchor.precondition, **{field: "0" * 64}))
    with pytest.raises(AgentGitError) as error:
        model_templates(bad, config, snapshot, no_blob)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_existing_prompt_rule_is_not_appended_again(inputs):
    anchor, config, snapshot, _ = inputs
    config = evolve(config, prompt=config.prompt + PROMPT_RULES[0][1])
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            config_ref=object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json"))),
        ),
    )
    result = model_templates(anchor, config, snapshot, no_blob)
    assert "prompt-verified-sources/v1:already_present" in result.skipped
    assert len(result.patches) == 2


def test_long_prompt_records_nonapplicability(inputs):
    anchor, config, snapshot, _ = inputs
    config = evolve(config, prompt="甲" * 16000)
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            config_ref=object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json"))),
        ),
    )
    result = model_templates(anchor, config, snapshot, no_blob)
    assert result.skipped == (
        "prompt-verified-sources/v1:prompt_limit",
        "prompt-required-artifacts/v1:prompt_limit",
    )
    assert len(result.patches) == 1


@pytest.mark.parametrize(
    "status", [RunStatus.OUTCOME_UNKNOWN, RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED]
)
def test_unsafe_checkpoint_is_rejected_before_proposal(inputs, status):
    anchor, config, snapshot, _ = inputs
    state = AgentState.model_validate_json(encode(snapshot.data))
    snapshot = evolve(snapshot, data=evolve(state, status=status).model_dump(mode="json"))
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            state_ref=object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json"))),
        ),
    )
    with pytest.raises(AgentGitError) as error:
        model_templates(anchor, config, snapshot, no_blob)
    assert error.value.code == ErrorCode.FORBIDDEN


def test_singletons_then_compatible_ordered_pairs_even_if_all_singletons_fail(inputs):
    generated = batch(inputs)
    space = freeze_space(inputs[3], generated.patches, source_reason="定位未达标，使用完整候选")
    proposals = list(enumerate_proposals(space))
    assert proposals[:3] == [(patch,) for patch in generated.patches]
    assert len(proposals) == 7
    prompt, other_prompt, plan = generated.patches
    assert proposals[3:] == [
        (prompt, plan),
        (other_prompt, plan),
        (plan, prompt),
        (plan, other_prompt),
    ]
    assert proposal_key(space.binding, (prompt, plan)) != proposal_key(
        space.binding, (plan, prompt)
    )
    assert list(enumerate_proposals(evolve(space, max_depth=1))) == proposals[:3]


def test_repeated_effect_ignores_explanation_but_trials_remain_distinct(inputs):
    patch = batch(inputs).patches[0]
    changed = evolve(
        patch,
        intervention=evolve(
            patch.intervention,
            provenance=evolve(patch.intervention.provenance, rationale="另一条解释"),
        ),
    )
    assert patch_ref(patch) == patch_ref(changed)
    space = freeze_space(inputs[3], (patch, changed, patch), source_reason="完整候选")
    assert space.patches == (patch,)
    proposal = proposal_key(inputs[3], (patch,))
    search = str(uuid4())
    assert trial_key(search, proposal, "screen", 0) == trial_key(search, proposal, "screen", 0)
    assert (
        len(
            {
                trial_key(search, proposal, "screen", 0),
                trial_key(search, proposal, "screen", 1),
                trial_key(search, proposal, "confirm", 0),
                trial_key(str(uuid4()), proposal, "screen", 0),
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment_ref", "c" * 64),
        ("policy_version", "bounded-templates/v2"),
        ("repository_id", str(uuid4())),
    ],
)
def test_dedup_binds_environment_policy_and_project(inputs, field, value):
    patch = batch(inputs).patches[0]
    assert proposal_key(inputs[3], (patch,)) != proposal_key(
        evolve(inputs[3], **{field: value}),
        (patch,),
    )


@pytest.mark.parametrize(
    "field,value",
    [("base_commit", "c" * 64), ("validator_ref", "c" * 64), ("failed_run_id", str(uuid4()))],
)
def test_cross_origin_patch_is_rejected(inputs, field, value):
    with pytest.raises(AgentGitError):
        proposal_key(evolve(inputs[3], **{field: value}), (batch(inputs).patches[0],))


def test_conflicting_fields_do_not_use_last_write_wins(inputs):
    first, second, _ = batch(inputs).patches
    with pytest.raises(AgentGitError) as error:
        validate_patches(inputs[3], (first, second))
    assert error.value.code == ErrorCode.VALIDATION


def test_finite_cutoff_records_omissions_and_duplicate_stream_is_bounded(inputs):
    patch = batch(inputs).patches[0]
    patches = tuple(
        evolve(
            patch,
            intervention=evolve(
                patch.intervention,
                payload=PromptPatch(prompt=f"固定候选 {index}"),
            ),
        )
        for index in range(10)
    )
    space = freeze_space(inputs[3], patches, source_reason="完整候选")
    assert len(space.patches) == 8
    assert space.excluded_patch_refs == tuple(patch_ref(item) for item in patches[8:])
    with pytest.raises(AgentGitError) as error:
        freeze_space(inputs[3], repeat(patch), source_reason="完整候选")
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


def test_recorded_observation_is_test_only_and_write_tools_are_forbidden(inputs):
    anchor, *_ = inputs
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            occurrence=evolve(
                anchor.precondition.occurrence,
                provider="tool:source_read/v1",
            ),
        ),
    )
    patch = observation_templates(anchor, (str(uuid4()),)).patches[0]
    assert patch.intervention.effect_policy == "test_only"
    assert patch.intervention.payload.test_only is True
    validate_patches(inputs[3], (patch,))
    bad = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            occurrence=evolve(
                anchor.precondition.occurrence,
                provider="tool:artifact_write/v1",
            ),
        ),
    )
    with pytest.raises(AgentGitError):
        observation_templates(bad, (str(uuid4()),))
    with pytest.raises(ValidationError):
        observation_templates(anchor, ("invalid-call",))


@pytest.mark.parametrize("field", ["validator", "tools", "execution_policy", "path"])
def test_patch_schema_rejects_hidden_permission_or_oracle_changes(inputs, field):
    data = batch(inputs).patches[0].model_dump(mode="json")
    data["intervention"]["payload"][field] = "forbidden"
    with pytest.raises(ValidationError):
        RepairPatch.model_validate_json(encode(data))


def test_plan_cycle_and_wrong_template_type_are_rejected(inputs):
    with pytest.raises(ValidationError):
        PlanPatch(
            plan=Plan(
                nodes=(
                    PlanNode(key="a", kind="search", dependencies=("b",)),
                    PlanNode(key="b", kind="write_report", dependencies=("a",)),
                )
            )
        )
    with pytest.raises(ValidationError):
        evolve(batch(inputs).patches[0], template="plan-required-artifacts/v1")


def rebind(inputs, state):
    anchor, config, snapshot, binding = inputs
    snapshot = evolve(snapshot, data=state.model_dump(mode="json"))
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            state_ref=object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json"))),
        ),
    )
    return anchor, config, snapshot, binding


def test_pending_tool_is_never_silently_cleared_by_a_model_template(inputs):
    state = AgentState.model_validate_json(encode(inputs[2].data))
    state = evolve(
        state,
        pending_tool=ToolDecision(
            node_key="report",
            tool="artifact_write",
            arguments={"path": "report.md"},
        ),
    )
    with pytest.raises(AgentGitError) as error:
        batch(rebind(inputs, state))
    assert error.value.code == ErrorCode.FORBIDDEN


def test_completed_plan_and_absent_plan_are_explicit_nonapplicability(inputs):
    state = AgentState.model_validate_json(encode(inputs[2].data))
    result = batch(rebind(inputs, evolve(state, plan=None, completed=())))
    assert result.skipped == ("plan-required-artifacts/v1:no_plan",)
    plan = evolve(
        state.plan, nodes=(*state.plan.nodes, PlanNode(key="sources", kind="write_sources"))
    )
    result = batch(rebind(inputs, evolve(state, plan=plan)))
    assert result.skipped == ("plan-required-artifacts/v1:already_present",)


def test_finite_plan_limit_and_node_name_collision(inputs):
    state = AgentState.model_validate_json(encode(inputs[2].data))
    full = Plan(nodes=tuple(PlanNode(key=f"n-{index}", kind="search") for index in range(64)))
    result = batch(rebind(inputs, evolve(state, plan=full, completed=())))
    assert result.skipped == ("plan-required-artifacts/v1:node_limit",)
    plan = evolve(
        state.plan, nodes=(*state.plan.nodes, PlanNode(key="repair-write-sources", kind="search"))
    )
    result = batch(rebind(inputs, evolve(state, plan=plan)))
    assert result.patches[-1].intervention.payload.plan.nodes[-1].key == "repair-write-sources-1"


def test_templates_do_not_add_missing_permissions(inputs):
    anchor, config, snapshot, _ = inputs
    config = evolve(config, tools=("corpus_search", "source_read"))
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            config_ref=object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json"))),
        ),
    )
    with pytest.raises(AgentGitError) as error:
        model_templates(anchor, config, snapshot, no_blob)
    assert error.value.code == ErrorCode.FORBIDDEN


def test_tool_observation_conflicts_use_source_occurrence_not_replacement_id(inputs):
    anchor, *_ = inputs
    anchor = evolve(
        anchor,
        precondition=evolve(
            anchor.precondition,
            occurrence=evolve(
                anchor.precondition.occurrence,
                provider="tool:source_read/v1",
            ),
        ),
    )
    first, second = observation_templates(anchor, (str(uuid4()), str(uuid4()))).patches
    with pytest.raises(AgentGitError) as error:
        validate_patches(inputs[3], (first, second))
    assert error.value.code == ErrorCode.VALIDATION
    second = evolve(
        second,
        intervention=evolve(
            second.intervention,
            target_event_id=8,
            precondition=evolve(
                second.intervention.precondition,
                occurrence=evolve(
                    second.intervention.precondition.occurrence,
                    logical_step_id="another-read",
                ),
            ),
        ),
    )
    validate_patches(inputs[3], (first, second))


def test_deserialized_space_cannot_bypass_origin_or_duplicate_checks(inputs):
    patch = batch(inputs).patches[0]
    space = RepairSpace(
        binding=inputs[3],
        patches=(patch, patch),
        candidate_source="full_candidates_fallback",
        source_reason="完整列表",
    )
    with pytest.raises(AgentGitError):
        next(enumerate_proposals(space))
    changed = evolve(
        patch,
        intervention=evolve(
            patch.intervention,
            precondition=evolve(
                patch.intervention.precondition,
                state_ref="0" * 64,
            ),
        ),
    )
    with pytest.raises(ValidationError):
        freeze_space(inputs[3], (patch, changed), source_reason="不匹配状态")
