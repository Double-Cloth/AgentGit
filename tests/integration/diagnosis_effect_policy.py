"""只读取公开候选与安全上下文的固定效果补丁，不接收标签、划分或成败。"""

from agentgit.adapters.mock import research_plan
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, AgentState, PlanNode, evolve

POLICY = "first-bindable-in-frozen-order/public-plan-else-prompt/v1"
PROMPT_SUFFIX = "\n完成前逐项核验报告、已读来源及原文引文，保持冻结时间与结构版本。"
MAX_CONTEXTS = 100


def select_context(manager, repository, identity, stored, validator):
    attempts = []
    candidates = stored["report"]["diagnosis"]["candidates"]
    for candidate in candidates[:MAX_CONTEXTS]:
        event_id = candidate["event"]["event_id"]
        try:
            bound = manager.debug_candidate_context(repository, identity, event_id, validator)
        except AgentGitError as error:
            attempts.append(dict(event_id=event_id, rank=candidate["rank"], error=error.code.value))
            continue
        attempts.append(dict(event_id=event_id, rank=candidate["rank"], error=None))
        return bound, attempts
    return None, attempts


def fixed_payload(context, snapshot):
    config = parse_contract(encode(context["config"]), AgentConfig)
    state = parse_contract(encode(snapshot["data"]), AgentState)
    if state.plan is None:
        return dict(kind="PLAN_PATCH", plan=research_plan(config.tools).model_dump(mode="json"))
    writers = [node for node in state.plan.nodes if node.kind == "write_report"]
    if writers and not any(node.kind == "write_sources" for node in state.plan.nodes):
        key = "write-sources"
        if key not in {node.key for node in state.plan.nodes}:
            extra = PlanNode(key=key, kind="write_sources", dependencies=(writers[-1].key,))
            return dict(
                kind="PLAN_PATCH",
                plan=evolve(state.plan, nodes=(*state.plan.nodes, extra)).model_dump(mode="json"),
            )
    return dict(kind="PROMPT_PATCH", prompt=config.prompt + PROMPT_SUFFIX)
