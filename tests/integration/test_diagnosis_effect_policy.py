"""公开上下文策略不读取标签，且补计划时不重写已有或完成节点。"""

from copy import deepcopy
from types import SimpleNamespace

from diagnosis_effect_policy import PROMPT_SUFFIX, fixed_payload, select_context

from agentgit.adapters.mock import research_plan
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, AgentState, Plan, ResearchGoal, evolve


def inputs(plan):
    return dict(config=AgentConfig().model_dump(mode="json")), dict(
        data=AgentState(
            goal=ResearchGoal(text="比较三个 fixture 框架"),
            plan=plan,
            completed=("search",) if plan else (),
        ).model_dump(mode="json")
    )


def test_missing_output_plan_addition_preserves_all_existing_and_completed_nodes():
    original = evolve(
        research_plan(), nodes=tuple(n for n in research_plan().nodes if n.kind != "write_sources")
    )
    context, snapshot = inputs(original)
    before = deepcopy(snapshot)
    value = fixed_payload(context, snapshot)
    patched = parse_contract(encode(value["plan"]), Plan)
    assert value["kind"] == "PLAN_PATCH" and patched.nodes[:-1] == original.nodes
    assert patched.nodes[-1].kind == "write_sources" and patched.nodes[-1].dependencies == (
        "write-report",
    )
    assert snapshot == before and snapshot["data"]["completed"] == ["search"]


def test_existing_plan_uses_same_prompt_regardless_of_untrusted_label_metadata():
    context, snapshot = inputs(research_plan())
    first = fixed_payload({**context, "case_id": "D01", "annotation": {"repair": "plan"}}, snapshot)
    other = fixed_payload(
        {**context, "case_id": "D20", "annotation": {"repair": "prompt"}}, snapshot
    )
    assert (
        first
        == other
        == dict(kind="PROMPT_PATCH", prompt=context["config"]["prompt"] + PROMPT_SUFFIX)
    )


def test_initial_plan_does_not_depend_on_case_or_success_metadata():
    context, snapshot = inputs(None)
    value = fixed_payload(context, snapshot)
    assert value["kind"] == "PLAN_PATCH"
    assert parse_contract(encode(value["plan"]), Plan) == research_plan()
    assert snapshot["data"]["plan"] is None


def test_candidate_order_and_rejections_remain_visible_without_selecting_by_gold():
    seen = []

    def context(repo, identity, event, validator):
        seen.append(event)
        if event == 10:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "缺证据")
        return {"selected": event}

    stored = dict(
        report=dict(
            diagnosis=dict(
                candidates=[
                    dict(event=dict(event_id=10), rank=1),
                    dict(event=dict(event_id=20), rank=4),
                    dict(event=dict(event_id=30), rank=4),
                ]
            )
        )
    )
    result, attempts = select_context(
        SimpleNamespace(debug_candidate_context=context), None, None, stored, "validator"
    )
    assert result == {"selected": 20} and seen == [10, 20]
    assert attempts[0]["error"] == "recording_missing" and attempts[1]["rank"] == 4
