"""只把已核验内容作为新任务输入，原轨迹、完成和效果均留在合并审计。"""

from collections.abc import Callable

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import MergedResearchState
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import LiveOverrides
from agentgit.replay.live import prepare_live
from agentgit.runtime.validation import validate_report


def initialize_merged(
    snapshot: StateSnapshot,
    config: AgentConfig,
    corpus: Corpus,
    read: Callable[[str, ObjectType], bytes],
) -> StateSnapshot:
    merged = parse_contract(encode(snapshot.data), MergedResearchState)
    if (
        set(config.tools) - set(merged.constraints.tools)
        or not config.limits.within(merged.constraints.limits)
        or (
            config.execution_policy is not None
            and config.execution_policy != merged.constraints.execution_policy
        )
        or corpus.evaluation_time != merged.constraints.evaluation_time
    ):
        raise AgentGitError(ErrorCode.FORBIDDEN, "新 Run 配置不能扩大合并约束或改变评估时间")
    if config.plan_template is not None and config.plan_template != merged.plan:
        raise AgentGitError(ErrorCode.PLAN_INVALID, "新配置模板不能隐式替换合并计划")
    if any(
        item.evidence.source != corpus.read(item.evidence.source.id) for item in merged.read_proofs
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "新验收语料与合并来源不同")
    files = {item.path: read(item.object_ref, ObjectType.BLOB) for item in snapshot.artifacts}
    projected = AgentState(
        goal=merged.goal,
        plan=merged.plan,
        plan_version=1 if merged.plan is not None else 0,
        status=RunStatus.RUNNING if merged.plan is not None else RunStatus.QUEUED,
        working_memory=merged.working_memory,
        usage=BudgetUsage(),
    )
    if not validate_report(projected, files, corpus).passed:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "新 Run 的合并输入未通过当前硬规则")
    candidate = StateSnapshot(data=projected.model_dump(mode="json"), artifacts=snapshot.artifacts)
    # 复用工作区容量、路径和完整计划工具检查；不套用其 Live 状态变换结果。
    prepare_live(candidate, config, LiveOverrides(), read)
    return candidate
