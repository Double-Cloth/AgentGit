"""受支持的原生 LangGraph 清单；不接受用户 Python 节点或可执行对象。"""

from typing import Annotated, Literal, TypedDict

from pydantic import Field, model_validator

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import MCPArguments, MCPBinding
from agentgit.core.mcp_runs import MCPProgram
from agentgit.core.models import Contract, EffectClass
from agentgit.core.research import BudgetLimits

LANGGRAPH_VERSION = "1.2.18"
LANGGRAPH_CHECKPOINT_VERSION = "4.3.1"


class LangGraphNode(Contract):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    kind: Annotated[str, Field(min_length=1, max_length=64)]


NODES = (
    LangGraphNode(id="fetch", kind="mcp_read"),
    LangGraphNode(id="review", kind="review_note"),
    LangGraphNode(id="publish", kind="publish"),
)
EDGES = (
    ("__start__", "fetch"),
    ("fetch", "review"),
    ("fetch", "__end__"),
    ("review", "publish"),
    ("publish", "__end__"),
)


class LangGraphDefinition(Contract):
    template: str = "mcp-read-review-publish/v1"
    nodes: Annotated[tuple[LangGraphNode, ...], Field(max_length=16)] = NODES
    edges: Annotated[tuple[tuple[str, str], ...], Field(max_length=32)] = EDGES
    pause_before: tuple[str, ...] = ("review",)

    @model_validator(mode="after")
    def supported_graph(self) -> "LangGraphDefinition":
        if (
            self.template != "mcp-read-review-publish/v1"
            or self.nodes != NODES
            or self.edges != EDGES
            or self.pause_before not in ((), ("review",))
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph 图、节点或边不在固定范围")
        return self


class LangGraphProgram(Contract):
    kind: Literal["langgraph_program/v1"] = "langgraph_program/v1"
    framework_version: Literal["1.2.18"] = "1.2.18"
    checkpoint_version: Literal["4.3.1"] = "4.3.1"
    graph: LangGraphDefinition = LangGraphDefinition()
    binding: MCPBinding
    request: MCPArguments
    review_note: Annotated[str, Field(max_length=2048)] = ""
    limits: BudgetLimits = BudgetLimits(
        max_steps=3,
        max_model_calls=1,
        max_tool_calls=1,
        max_replans=0,
        max_response_bytes=65536,
        max_artifact_bytes=262144,
        max_files=2,
    )

    @model_validator(mode="after")
    def fixed_permissions_and_budget(self) -> "LangGraphProgram":
        if self.binding.effect_class != EffectClass.READ_ONLY:
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph 首批只支持本地登记的只读工具"
            )
        MCPProgram(binding=self.binding, request=self.request, limits=self.limits)
        if self.limits.max_steps < 3 or self.limits.max_files < 2 or self.limits.max_replans != 0:
            raise ValueError("LangGraph 三节点程序必须使用匹配的固定预算")
        return self


class LangGraphValues(TypedDict):
    fetch_ref: str | None
    fetch_text: str
    review_note: str
    reviewed_text: str
    report: str
    error_code: str | None
    node_order: list[str]


def initial_values(program: LangGraphProgram) -> LangGraphValues:
    return {
        "fetch_ref": None,
        "fetch_text": "",
        "review_note": program.review_note,
        "reviewed_text": "",
        "report": "",
        "error_code": None,
        "node_order": [],
    }
