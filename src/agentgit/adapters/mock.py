"""由实际观察和工作记忆决定下一步的离线 Mock LLM。"""

from typing import Literal

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


def research_plan(tools: tuple[str, ...] = ()) -> Plan:
    reads = tuple(
        PlanNode(key=f"read-{index}", kind="read", dependencies=("search",), source_slot=index)
        for index in range(6)
    )
    execution: list[PlanNode] = []
    previous = "write-sources"
    recipes: tuple[
        tuple[
            Literal["code_exec", "terminal_exec"],
            Literal["write_execution_code", "write_execution_command"],
        ],
        ...,
    ] = (
        ("code_exec", "write_execution_code"),
        ("terminal_exec", "write_execution_command"),
    )
    for tool, preparation in recipes:
        if tool in tools:
            execution.extend(
                (
                    PlanNode(key=f"prepare-{tool}", kind=preparation, dependencies=(previous,)),
                    PlanNode(key=tool, kind=tool, dependencies=(f"prepare-{tool}",)),
                )
            )
            previous = tool
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
            *execution,
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
            decision = PlanDecision(plan=research_plan(request.available_tools))
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
            case "write_execution_code" | "write_execution_command":
                code = node.kind == "write_execution_code"
                tool, arguments = (
                    "artifact_write",
                    {
                        "schema_version": 1,
                        "path": "execution/statistics.py" if code else "execution/organize.sh",
                        "text": _STATISTICS_CODE if code else _ORGANIZE_COMMAND,
                        "media_type": "text/x-python" if code else "text/x-shellscript",
                    },
                )
            case "code_exec" | "terminal_exec":
                context = request.execution_context
                if context is None:
                    raise AgentGitError(ErrorCode.FORBIDDEN, "执行决策缺少后端文件和策略上下文")
                tool = node.kind
                arguments = {
                    "schema_version": 1,
                    "cwd": ".",
                    "timeout_ms": 30000,
                    "input_manifest_ref": context.input_manifest_ref,
                    "execution_policy_ref": context.execution_policy_ref,
                }
                if tool == "code_exec":
                    arguments.update(
                        language="python",
                        script_path="execution/statistics.py",
                        argv=["sources.json", "statistics.json"],
                    )
                else:
                    command = next(
                        (item for item in context.files if item.path == "execution/organize.sh"),
                        None,
                    )
                    if command is None:
                        raise AgentGitError(ErrorCode.PLAN_INVALID, "工作区缺少终端命令原文")
                    arguments["command_ref"] = command.object_ref
        return encode(
            ToolDecision(node_key=node.key, tool=tool, arguments=arguments).model_dump(mode="json")
        )


_STATISTICS_CODE = """import json
import sys
from pathlib import Path

# 输入来自已经生成的合成资料产物，不包含宿主验收器或答案。
report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
counts = {name: sum(claim["framework"] == name for claim in report["claims"])
          for name in report["frameworks"]}
result = {"schema_version": 1, "frameworks": len(counts), "claims": len(report["claims"]),
          "sources": len(report["sources"]), "claims_per_framework": counts}
text = json.dumps(result, ensure_ascii=False, sort_keys=True)+"\\n"
Path(sys.argv[2]).write_text(text, encoding="utf-8")
print("statistics written")
"""

_ORGANIZE_COMMAND = """set -eu
mkdir -p results
if [ -f statistics.json ]; then mv statistics.json results/statistics.json; fi
cp report.md results/report.md
printf '执行完成；内容来自固定合成资料。\\n' > results/execution.txt
printf 'artifacts organized\\n'
"""
