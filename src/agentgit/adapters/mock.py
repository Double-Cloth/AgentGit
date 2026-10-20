"""由实际观察和工作记忆决定下一步的离线 Mock LLM。"""

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import (
    FinalDecision,
    ModelRequest,
    Plan,
    PlanDecision,
    PlanNode,
    ToolDecision,
)
from agentgit.runtime.validation import render_report, report_from_memory


def research_plan() -> Plan:
    reads = tuple(
        PlanNode(key=f"read-{index}", kind="read", dependencies=("search",), source_slot=index)
        for index in range(6)
    )
    return Plan(
        nodes=(
            PlanNode(key="search", kind="search"),
            *reads,
            PlanNode(
                key="write-report",
                kind="write_report",
                dependencies=tuple(node.key for node in reads),
            ),
            PlanNode(key="write-sources", kind="write_sources", dependencies=("write-report",)),
        )
    )


class MockResearchModel:
    signature = object_digest(
        ObjectType.CALL, encode({"schema_version": 1, "model": "mock-research/v1"})
    )

    def invoke(self, payload: bytes) -> bytes:
        request = parse_contract(payload, ModelRequest)
        state = request.state
        if request.phase == "plan":
            decision = PlanDecision(plan=research_plan())
            return encode(decision.model_dump(mode="json"))
        if state.plan is None:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "执行前没有有效计划")
        ready = state.plan.ready(state.completed)
        if not ready:
            return encode(
                FinalDecision(summary="合成框架对比已完成，等待独立验收。").model_dump(mode="json")
            )
        node = ready[0]
        match node.kind:
            case "search":
                tool, arguments = (
                    "corpus_search",
                    {"schema_version": 1, "frameworks": list(state.goal.frameworks)},
                )
            case "read":
                if node.source_slot is None or node.source_slot >= len(state.search_results):
                    raise AgentGitError(ErrorCode.TOOL_EMPTY, "搜索未提供足够来源")
                tool, arguments = (
                    "source_read",
                    {"schema_version": 1, "source_id": state.search_results[node.source_slot]},
                )
            case "write_report" | "write_sources":
                report = report_from_memory(state, request.evaluation_time)
                text = (
                    render_report(report)
                    if node.kind == "write_report"
                    else encode(report.model_dump(mode="json")).decode("utf-8")
                )
                tool, arguments = (
                    "artifact_write",
                    {
                        "schema_version": 1,
                        "path": "report.md" if node.kind == "write_report" else "sources.json",
                        "text": text,
                        "media_type": "text/markdown"
                        if node.kind == "write_report"
                        else "application/json",
                    },
                )
        return encode(
            ToolDecision(node_key=node.key, tool=tool, arguments=arguments).model_dump(mode="json")
        )
