"""在原 Worker 上装配固定只读观测环境，保持生产权限、调用记账和验收。"""

from typing import Literal

from agentgit.core.canonical import decode, encode
from agentgit.worker import Worker

Observation = Literal["canonical", "group_rotated", "empty"]


class ObservationTools:
    """只改变搜索响应，保留原工具接口签名供显式观测替换核验。"""

    def __init__(self, delegate, observation: Observation):
        if observation not in ("canonical", "group_rotated", "empty"):
            raise ValueError("未知的固定观测环境")
        self.delegate, self.observation = delegate, observation

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def invoke(self, name, arguments):
        # 先走原 Registry 的权限、参数与工具调用，再应用预先声明的只读环境。
        result = self.delegate.invoke(name, arguments)
        if name != "corpus_search" or self.observation == "canonical":
            return result
        value = decode(result)
        ids = value["source_ids"]
        if self.observation == "empty" and len(arguments["frameworks"]) == 3:
            value["source_ids"] = []
        elif self.observation == "group_rotated":
            groups = ("Birch", "Cedar", "Aster")
            value["source_ids"] = [
                key
                for group in groups
                for key in ids
                if self.delegate.corpus.read(key).framework == group
            ]
        return encode(value)


def source_observation(condition) -> Observation:
    return "empty" if condition.rank == "empty_fallback" else "canonical"


def reference_observation(condition) -> Observation:
    return "group_rotated" if condition.rank in ("framework", "citation", "both") else "canonical"


def attach_observation(session, observation: Observation):
    session.tools = ObservationTools(session.tools, observation)
    return session


class ObservationWorker(Worker):
    """租约、恢复、Live/Hybrid、派发和结算仍全部继承生产 Worker。"""

    def __init__(self, *args, observation: Observation, **kwargs):
        super().__init__(*args, **kwargs)
        self.observation = observation

    def _attach(self, lease):
        session, origins, remaining = super()._attach(lease)
        attach_observation(session, self.observation)
        return session, origins, remaining
