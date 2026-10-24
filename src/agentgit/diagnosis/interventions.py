"""把冻结候选绑定到同一次模型调用的开始事件，不按相邻顺序猜测。"""

from agentgit.core.debug_reports import DebugReport, FaultCandidate
from agentgit.core.diagnosis import TraceNode
from agentgit.core.errors import AgentGitError, ErrorCode


def candidate_request(report: DebugReport, event_id: int) -> tuple[FaultCandidate, TraceNode]:
    if type(event_id) is not int or not 1 <= event_id <= 9_007_199_254_740_991:
        raise AgentGitError(ErrorCode.VALIDATION, "候选必须使用正整数事件 ID")
    candidates = [item for item in report.diagnosis.candidates if item.event.event_id == event_id]
    if len(candidates) != 1:
        raise AgentGitError(ErrorCode.NOT_FOUND, "事件不是当前报告的唯一候选")
    candidate = candidates[0]
    nodes = {item.event.key: item for item in report.failed.nodes}
    result = nodes[candidate.event.key]
    requests = [
        nodes[edge.source.key]
        for edge in report.failed.edges
        if edge.target == candidate.event and edge.basis == "call_occurrence"
    ]
    gaps = {item.event.key for item in report.failed.gaps}
    if (
        len(requests) != 1
        or result.phase != "call.result"
        or result.occurrence is None
        or not result.occurrence.provider.startswith("llm:")
        or not result.payload_verified
        or result.observation_ref is None
        or result.event.key in gaps
    ):
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "候选缺少唯一完整的模型调用证据")
    request = requests[0]
    if (
        request.phase != "call.request"
        or request.event_type != "llm.requested"
        or request.occurrence != result.occurrence
        or request.event.event_id is None
        or not request.payload_verified
        or request.event.key in gaps
    ):
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "候选开始事件与模型观测不能精确绑定")
    return candidate, request
