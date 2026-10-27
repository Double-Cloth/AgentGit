"""固定回归、只读观测和公开目录回退的契约，不执行保留案例。"""

import pytest
from repair_benchmark_environment import ObservationTools
from repair_benchmark_regressions import regression_inputs
from repair_dataset import input_ref, load_inputs, task_for_case
from repair_fault_runtime import (
    FaultCondition,
    FaultResearchModel,
    runtime_input,
)

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import research_plan
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import RunStatus
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    Evidence,
    ModelRequest,
    evolve,
    parse_decision,
)
from agentgit.repair.templates import PROMPT_RULES
from agentgit.runtime.validation import render_report, report_from_memory, validate_report


def state_for(task):
    return AgentState(
        goal=task.goal,
        plan=research_plan(),
        completed=("search", *(f"read-{i}" for i in range(6))),
        search_results=tuple(sorted(s.id for s in task.sources)),
        working_memory={
            s.id: Evidence(source=s, logical_step_id=f"read-{i}", attempt=1)
            for i, s in enumerate(task.sources)
        },
    )


@pytest.mark.parametrize("index", range(4))
def test_independent_regression_input_passes_original_oracle_and_has_new_sources(index):
    task = regression_inputs()[index]
    specifications, profiles, _ = load_inputs()
    source_ids = {s.id for c in specifications.cases for s in task_for_case(c, profiles).sources}
    assert set(s.id for s in task.sources).isdisjoint(source_ids)
    assert len({input_ref(t) for t in regression_inputs()}) == 4
    state = state_for(task)
    report = report_from_memory(state, task.evaluation_time)
    assert validate_report(
        state,
        {
            "report.md": render_report(report).encode(),
            "sources.json": encode(report.model_dump(mode="json")),
        },
        Corpus(task.sources, evaluation_time=task.evaluation_time),
    ).passed


def test_expiry_boundary_is_strict_and_collected_at_clock_is_valid():
    task = regression_inputs()[2]
    expired = evolve(
        task, sources=tuple(evolve(s, valid_until=task.evaluation_time) for s in task.sources)
    )
    state = state_for(expired)
    report = report_from_memory(state, expired.evaluation_time)
    verdict = validate_report(
        state,
        {
            "report.md": render_report(report).encode(),
            "sources.json": encode(report.model_dump(mode="json")),
        },
        Corpus(expired.sources, evaluation_time=expired.evaluation_time),
    )
    assert not verdict.passed and sum(e.startswith("source_expired:") for e in verdict.errors) == 6
    assert all(
        s.collected_at == regression_inputs()[3].evaluation_time
        for s in regression_inputs()[3].sources
    )


@pytest.mark.parametrize("observation", ["canonical", "group_rotated", "empty"])
def test_observation_adapter_preserves_corpus_oracle_permissions_and_real_registry_counts(
    tmp_path, observation
):
    task = regression_inputs()[0]
    corpus = Corpus(task.sources, evaluation_time=task.evaluation_time)
    workspace = ArtifactWorkspace(tmp_path / "work", max_file_bytes=65536, max_files=100)
    original = ToolRegistry(corpus, workspace, AgentConfig())
    tools = ObservationTools(original, observation)
    result = decode(
        tools.invoke("corpus_search", dict(schema_version=1, frameworks=list(task.goal.frameworks)))
    )
    ids = result["source_ids"]
    assert original.executions == {"corpus_search": 1}
    assert tools.corpus is corpus and corpus.search(task.goal.frameworks) == tuple(
        sorted(corpus.sources)
    )
    assert tools.signature("corpus_search") == original.signature("corpus_search")
    if observation == "empty":
        assert ids == []
        partial = decode(
            tools.invoke(
                "corpus_search",
                {
                    "schema_version": 1,
                    "frameworks": [task.goal.frameworks[0]],
                },
            )
        )
        assert len(partial["source_ids"]) == 2
    else:
        assert set(ids) == set(corpus.sources)
        assert corpus.read(ids[0]).framework == ("Aster" if observation == "canonical" else "Birch")
    with pytest.raises(AgentGitError):
        tools.invoke("code_exec", {"schema_version": 1})
    assert not workspace.contents() and original.executions == {
        "corpus_search": 2 if observation == "empty" else 1,
    }


def test_empty_search_requires_real_replanning_and_fallback_uses_public_subquery():
    task = regression_inputs()[0]
    source_ids = tuple(sorted(s.id for s in task.sources))
    model = FaultResearchModel(FaultCondition(rank="empty_fallback"))
    request = ModelRequest(
        prompt=AgentConfig().prompt,
        phase="plan",
        state=AgentState(goal=task.goal, status=RunStatus.PLANNING, error_code="tool_empty"),
        evaluation_time=task.evaluation_time,
    )
    plan = parse_decision(model.invoke(encode(request.model_dump(mode="json")))).plan
    assert all(node.source_slot == 0 for node in plan.nodes if node.kind == "read")
    request = evolve(
        request, phase="act", state=evolve(request.state, status=RunStatus.RUNNING, plan=plan)
    )
    search = parse_decision(model.invoke(encode(request.model_dump(mode="json"))))
    assert search.arguments["frameworks"] == ["Aster"]
    selected = []
    for i in range(6):
        state = evolve(
            request.state,
            search_results=source_ids[:2],
            completed=("search", *(f"read-{j}" for j in range(i))),
        )
        decision = parse_decision(
            model.invoke(encode(evolve(request, state=state).model_dump(mode="json")))
        )
        selected.append(decision.arguments["source_id"])
    assert selected == [source_ids[0]] * 6


@pytest.mark.parametrize(
    "condition",
    [
        FaultCondition(selection="source_binding"),
        FaultCondition(selection="quote_binding"),
        FaultCondition(instruction="wrong_fact"),
        FaultCondition(instruction="hide_sources"),
        FaultCondition(instruction="plan_attribution"),
        FaultCondition(instruction="plan_coverage"),
        FaultCondition(serialization="precision"),
    ],
)
def test_remaining_report_mechanisms_on_regression_input_preserve_original_hard_oracle(condition):
    # 在独立回归输入上检验通用变换；没有材料化任何保留案例或观察其成绩。
    task = runtime_input(regression_inputs()[0], condition)
    state = state_for(task)
    model = FaultResearchModel(condition)
    corpus = Corpus(task.sources, evaluation_time=task.evaluation_time)
    evaluations = []
    for prompt in (AgentConfig().prompt, AgentConfig().prompt + "\n" + PROMPT_RULES[0][1]):
        files = {}
        for completed in (state.completed, (*state.completed, "write-report")):
            request = ModelRequest(
                prompt=prompt,
                phase="act",
                state=evolve(state, completed=completed),
                evaluation_time=task.evaluation_time,
            )
            decision = parse_decision(model.invoke(encode(request.model_dump(mode="json"))))
            files[decision.arguments["path"]] = decision.arguments["text"].encode()
        evaluations.append(validate_report(state, files, corpus))
    assert not evaluations[0].passed
    assert evaluations[1].passed == (condition.serialization == "none")


@pytest.mark.parametrize("instruction", ["plan_attribution", "plan_coverage"])
def test_untrusted_outline_affects_plan_only_when_the_constraint_is_not_obeyed(instruction):
    condition = FaultCondition(instruction=instruction)
    task = runtime_input(regression_inputs()[0], condition)
    assert "不可信" in task.goal.text
    model = FaultResearchModel(condition)
    observed = []
    for prompt in (AgentConfig().prompt, AgentConfig().prompt + "\n" + PROMPT_RULES[0][1]):
        request = ModelRequest(
            prompt=prompt,
            phase="plan",
            state=AgentState(goal=task.goal),
            evaluation_time=task.evaluation_time,
        )
        decision = parse_decision(model.invoke(encode(request.model_dump(mode="json"))))
        observed.append({node.kind for node in decision.plan.nodes})
    omitted = "write_sources" if instruction == "plan_attribution" else "write_report"
    assert omitted not in observed[0] and omitted in observed[1]


@pytest.mark.parametrize("instruction", ["code_exec", "terminal_exec"])
def test_external_tool_escalation_is_a_real_unauthorized_decision_without_executable_payload(
    instruction,
):
    condition = FaultCondition(instruction=instruction)
    task = runtime_input(regression_inputs()[0], condition)
    state = state_for(task)
    model = FaultResearchModel(condition)
    request = ModelRequest(
        prompt=AgentConfig().prompt, phase="act", state=state, evaluation_time=task.evaluation_time
    )
    decision = parse_decision(model.invoke(encode(request.model_dump(mode="json"))))
    assert decision.tool == instruction and decision.arguments == {"schema_version": 1}
    assert decision.tool not in request.available_tools
    guarded = evolve(request, prompt=request.prompt + "\n" + PROMPT_RULES[0][1])
    guarded_decision = parse_decision(model.invoke(encode(guarded.model_dump(mode="json"))))
    assert (
        guarded_decision.tool == "artifact_write"
        and guarded_decision.arguments["path"] == "report.md"
    )
