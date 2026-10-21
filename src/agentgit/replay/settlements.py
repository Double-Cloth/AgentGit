"""可信核对的纯重放输入；物理查询与证明读取由服务层完成。"""

from dataclasses import dataclass

from agentgit.core.research import AgentState
from agentgit.replay.recorded import Recording


@dataclass(frozen=True)
class RecordedSettlement:
    at_commit: str
    source_commit: str
    before: AgentState
    record: Recording
