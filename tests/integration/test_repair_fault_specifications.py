"""故障 v2 在未运行保留任务前核对机制隔离，不从运行结果修改分母。"""

import json
from copy import deepcopy

import pytest
from repair_benchmark_regressions import regression_inputs
from repair_fault_runtime import FaultResearchModel, runtime_input
from repair_fault_specifications import FAULTS, FaultSpecifications, load_faults
from test_repair_benchmark_inputs import state_for

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, AgentState, ModelRequest, evolve, parse_decision
from agentgit.repair.templates import PROMPT_RULES
from agentgit.runtime.validation import ResearchReport


def request_for(condition):
    public = runtime_input(regression_inputs()[0], condition)
    return public, ModelRequest(
        prompt=AgentConfig().prompt,
        phase="act",
        state=state_for(public),
        evaluation_time=public.evaluation_time,
    )


def decision(model, request):
    return parse_decision(model.invoke(encode(request.model_dump(mode="json"))))


def test_v2_keeps_thirty_cases_twelve_targets_and_one_held_instruction_family():
    specs = load_faults()
    assert len(specs.cases) == 30 and len(specs.families) == 11
    family = next(f for f in specs.families if f.key == "untrusted-source-instruction")
    assert family.split == "heldout" and family.members == (
        "R23",
        "R24",
        "R25",
        "R26",
        "R29",
        "R30",
    )
    assert all(c.condition.instruction == "none" for c in specs.cases if c.split == "train")
    assert sum(c.split == "train" and c.declared_repairable for c in specs.cases) == 12


@pytest.mark.parametrize("case_id", ["R19", "R20"])
def test_development_path_fault_is_independent_of_source_instructions_and_obeys_artifact_constraint(
    case_id,
):
    condition = next(c.condition for c in load_faults().cases if c.id == case_id)
    public, request = request_for(condition)
    assert public == regression_inputs()[0]
    model = FaultResearchModel(condition)
    expected = (
        "../agentgit-evaluation-blocked.md"
        if case_id == "R19"
        else "/agentgit-evaluation-blocked.md"
    )
    assert decision(model, request).arguments["path"] == expected
    evidence_guard = evolve(request, prompt=request.prompt + "\n" + PROMPT_RULES[0][1])
    assert decision(model, evidence_guard).arguments["path"] == expected
    artifact_guard = evolve(request, prompt=request.prompt + "\n" + PROMPT_RULES[1][1])
    assert decision(model, artifact_guard).arguments["path"] == "report.md"


def test_real_quote_join_is_distinct_from_fabrication_and_invariant_to_search_rank():
    specs = load_faults()
    condition = next(c.condition for c in specs.cases if c.id == "R22")
    public, request = request_for(condition)
    request = evolve(
        request, state=evolve(request.state, completed=(*request.state.completed, "write-report"))
    )
    model = FaultResearchModel(condition)
    report = parse_contract(decision(model, request).arguments["text"].encode(), ResearchReport)
    first = report.claims[0]
    source = next(s for s in public.sources if s.id == first.source_id)
    assert first.quote in source.body
    assert not any(
        (f.dimension, f.value, f.quote) == (first.dimension, first.value, first.quote)
        for f in source.facts
    )
    changed = evolve(
        request,
        state=evolve(request.state, search_results=tuple(reversed(request.state.search_results))),
    )
    assert decision(model, request) == decision(model, changed)
    invented = next(c.condition for c in specs.cases if c.id == "R04")
    invented_report = parse_contract(
        decision(FaultResearchModel(invented), request).arguments["text"].encode(), ResearchReport
    )
    assert not any(invented_report.claims[0].quote in s.body for s in public.sources)


def test_instruction_plan_trigger_is_absent_from_mechanical_development_plan_fault():
    specs = load_faults()
    public = regression_inputs()[0]
    instruction = next(c.condition for c in specs.cases if c.id == "R25")
    mechanical = next(c.condition for c in specs.cases if c.id == "R07")
    request = ModelRequest(
        prompt=AgentConfig().prompt,
        phase="plan",
        state=AgentState(goal=public.goal),
        evaluation_time=public.evaluation_time,
    )
    assert any(
        n.kind == "write_sources"
        for n in decision(FaultResearchModel(instruction), request).plan.nodes
    )
    assert not any(
        n.kind == "write_sources"
        for n in decision(FaultResearchModel(mechanical), request).plan.nodes
    )


@pytest.mark.parametrize(
    "damage", ["missing", "denominator", "cross_split", "instruction_alias", "pre_filled_score"]
)
def test_runtime_specification_rejects_task_or_mechanism_leakage(damage):
    value = json.loads(FAULTS.read_text(encoding="utf-8"))
    broken = deepcopy(value)
    if damage == "missing":
        broken["cases"].pop()
    elif damage == "denominator":
        broken["cases"][0]["declared_repairable"] = False
    elif damage == "cross_split":
        broken["families"][-1]["split"] = "train"
    elif damage == "instruction_alias":
        broken["cases"][18]["condition"]["unsafe_path"] = "none"
        broken["cases"][18]["condition"]["instruction"] = "parent_path"
    else:
        broken["cases"][0]["expected_passed"] = True
    with pytest.raises(AgentGitError):
        parse_contract(encode(broken), FaultSpecifications)
