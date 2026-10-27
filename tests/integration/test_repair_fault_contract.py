"""开发故障的构造契约；工作记忆测试不是 Run、搜索或研究成绩。"""

import pytest
from repair_dataset import load_inputs, task_for_case
from repair_fault_runtime import (
    PATTERNS,
    FaultCondition,
    FaultResearchModel,
    condition_for,
    runtime_input,
)

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel, research_plan
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    Evidence,
    ModelRequest,
    evolve,
    parse_decision,
)
from agentgit.repair.templates import PROMPT_RULES
from agentgit.runtime.validation import validate_report


def prepared(case_id):
    specifications, profiles, _ = load_inputs()
    case = next(c for c in specifications.cases if c.id == case_id)
    assert case.split == "train"
    condition = condition_for(case)
    task = runtime_input(task_for_case(case, profiles), condition)
    state = AgentState(
        goal=task.goal,
        plan=research_plan(),
        completed=(
            "search",
            *(f"read-{i}" for i in range(6)),
        ),
        search_results=tuple(sorted(s.id for s in task.sources)),
        working_memory={
            s.id: Evidence(source=s, logical_step_id=f"read-{i}", attempt=1)
            for i, s in enumerate(task.sources)
        },
    )
    request = ModelRequest(
        prompt=AgentConfig().prompt, phase="act", state=state, evaluation_time=task.evaluation_time
    )
    return case, condition, task, request


def outputs(model, request):
    files = {}
    for completed in (request.state.completed, (*request.state.completed, "write-report")):
        decision = parse_decision(
            model.invoke(
                encode(
                    evolve(
                        request,
                        state=evolve(request.state, completed=completed),
                    ).model_dump(mode="json")
                )
            )
        )
        files[decision.arguments["path"]] = decision.arguments["text"].encode()
    return files


def quality_request(request):
    return evolve(request, prompt=request.prompt + "\n" + PROMPT_RULES[0][1])


def test_all_declared_patterns_have_a_mechanism_without_exposing_metadata_to_model():
    specifications, _, _ = load_inputs()
    assert set(PATTERNS) == {c.pattern for c in specifications.cases}
    for case in specifications.cases:
        condition = condition_for(case)
        assert condition == condition_for(
            evolve(case, declared_repairable=not case.declared_repairable)
        )
        assert set(condition.model_dump()) == {
            "schema_version",
            "kind",
            "omit",
            "selection",
            "rank",
            "serialization",
            "instruction",
        }
        for forbidden in (case.id, case.pattern, case.family, "declared_repairable", "heldout"):
            assert forbidden.encode() not in encode(condition.model_dump(mode="json"))


@pytest.mark.parametrize("case_id", [f"R{i:02}" for i in range(4, 19)])
def test_development_report_fault_is_rejected_by_original_oracle_and_control_passes(case_id):
    _, condition, task, request = prepared(case_id)
    corpus = Corpus(task.sources, evaluation_time=task.evaluation_time)
    assert validate_report(request.state, outputs(MockResearchModel(), request), corpus).passed
    assert not validate_report(
        request.state, outputs(FaultResearchModel(condition), request), corpus
    ).passed


@pytest.mark.parametrize("case_id", [f"R{i:02}" for i in range(1, 13)])
def test_development_plan_omissions_and_selection_constraints_have_separate_repair_dimensions(
    case_id,
):
    _, condition, task, request = prepared(case_id)
    model = FaultResearchModel(condition)
    plan_request = evolve(request, phase="plan", state=AgentState(goal=task.goal))
    decision = parse_decision(model.invoke(encode(plan_request.model_dump(mode="json"))))
    required = {"write_report", "write_sources"}
    assert ({n.kind for n in decision.plan.nodes} >= required) == (condition.omit == "none")
    repaired = quality_request(request)
    # 此处显式保留完整计划，仅核对选择机制；不声称搜索器已找到修复。
    assert validate_report(
        repaired.state,
        outputs(model, repaired),
        Corpus(task.sources, evaluation_time=task.evaluation_time),
    ).passed


@pytest.mark.parametrize("case_id", ["R13", "R14", "R15"])
def test_rank_dependency_uses_real_observation_and_is_not_disabled_by_prompt(case_id):
    _, condition, task, request = prepared(case_id)
    model = FaultResearchModel(condition)
    request = quality_request(request)
    corpus = Corpus(task.sources, evaluation_time=task.evaluation_time)
    assert not validate_report(request.state, outputs(model, request), corpus).passed
    order = request.state.goal.frameworks[1:] + request.state.goal.frameworks[:1]
    ranked = tuple(
        s.id
        for name in order
        for s in sorted(task.sources, key=lambda s: s.id)
        if s.framework == name
    )
    changed = evolve(request, state=evolve(request.state, search_results=ranked))
    assert validate_report(changed.state, outputs(model, changed), corpus).passed


@pytest.mark.parametrize("case_id", ["R16", "R17", "R18"])
def test_serialization_failure_persists_after_both_prompt_constraints(case_id):
    _, condition, task, request = prepared(case_id)
    request = evolve(request, prompt=request.prompt + "\n" + "\n".join(r[1] for r in PROMPT_RULES))
    assert not validate_report(
        request.state,
        outputs(FaultResearchModel(condition), request),
        Corpus(task.sources, evaluation_time=task.evaluation_time),
    ).passed


@pytest.mark.parametrize("case_id", ["R19", "R20"])
def test_instruction_is_canonical_untrusted_data_with_unchanged_facts_and_strict_guard(case_id):
    case, condition, task, request = prepared(case_id)
    specifications, profiles, _ = load_inputs()
    original = task_for_case(next(c for c in specifications.cases if c.id == case.id), profiles)
    assert task.goal == original.goal and task != original
    assert task.sources[0].facts == original.sources[0].facts
    assert task.sources[0].body_sha256 != original.sources[0].body_sha256
    model = FaultResearchModel(condition)
    files = outputs(model, request)
    assert set(files) == (
        {"../agentgit-evaluation-blocked.md"}
        if case_id == "R19"
        else {"/agentgit-evaluation-blocked.md"}
    )
    guarded = quality_request(request)
    assert validate_report(
        guarded.state,
        outputs(model, guarded),
        Corpus(task.sources, evaluation_time=task.evaluation_time),
    ).passed


def test_model_is_stateless_deterministic_and_signature_binds_condition_and_source():
    _, condition, _, request = prepared("R06")
    payload = encode(request.model_dump(mode="json"))
    first, second = FaultResearchModel(condition), FaultResearchModel(condition)
    assert first.signature == second.signature
    assert first.invoke(payload) == first.invoke(payload) == second.invoke(payload)
    other = FaultResearchModel(FaultCondition(selection="unsupported"))
    assert first.signature != other.signature
    with pytest.raises(AgentGitError):
        parse_contract(
            encode({**condition.model_dump(mode="json"), "method": "repair"}), FaultCondition
        )
    with pytest.raises(ValueError):
        FaultCondition()
