"""按冻结配置选择模型；构造过程不读取凭据或发起请求。"""

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.openai import OpenAIResearchModel
from agentgit.core.research import AgentConfig
from agentgit.runtime.ports import ModelAdapter


def select_model(config: AgentConfig) -> ModelAdapter:
    if config.openai is None:
        return MockResearchModel()
    assert config.limits.max_cost_micro_usd is not None
    return OpenAIResearchModel(config.openai, cost_limit_micro_usd=config.limits.max_cost_micro_usd)
