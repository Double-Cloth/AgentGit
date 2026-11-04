"""固定 LangGraph 的原生执行边界；MCP 调用继续由既有网关执行或录制回放。"""

from importlib.metadata import PackageNotFoundError, version
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import StateSnapshot
from langsmith.run_helpers import tracing_context

from agentgit.adapters.mcp import MCPAdapter, MCPClient
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import (
    LANGGRAPH_CHECKPOINT_VERSION,
    LANGGRAPH_VERSION,
    LangGraphProgram,
    LangGraphValues,
    initial_values,
)
from agentgit.core.models import parse_contract
from agentgit.runtime.ports import CallGateway


class LangGraphAGJSONSerde:
    """只接受 AGJSON，不加载 Pickle、MsgPack 类型标签或 Python 对象。"""

    def dumps_typed(self, value: Any) -> tuple[str, bytes]:
        try:
            raw = encode(value)
        except AgentGitError:
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph checkpoint 含不支持的类型"
            ) from None
        if len(raw) > 1048576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "LangGraph 原生记录超过单值上限")
        return "agjson-v1", raw

    def loads_typed(self, value: tuple[str, bytes]) -> Any:
        if value[0] != "agjson-v1":
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph checkpoint 编码不受支持")
        if len(value[1]) > 1048576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "LangGraph 原生记录超过单值上限")
        return decode(value[1])


def check_framework_version() -> None:
    try:
        actual = (version("langgraph"), version("langgraph-checkpoint"))
    except PackageNotFoundError:
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph 固定依赖不可用") from None
    if actual != (LANGGRAPH_VERSION, LANGGRAPH_CHECKPOINT_VERSION):
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph 或 checkpoint 版本不匹配")


class LangGraphAdapter:
    def __init__(
        self,
        program: LangGraphProgram,
        gateway: CallGateway,
        *,
        thread_id: str,
        client: MCPClient | None = None,
        checkpointer: BaseCheckpointSaver[str] | None = None,
        step_boundaries: bool = False,
    ):
        check_framework_version()
        try:
            valid = str(UUID(thread_id)) == thread_id
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise AgentGitError(
                ErrorCode.VALIDATION, "LangGraph thread 必须使用当前 Run 的规范 UUID"
            )
        # 复制嵌套参数，调用者随后修改原 dict 不能改变已构造图的固定输入。
        program = parse_contract(encode(program.model_dump(mode="json")), LangGraphProgram)
        MCPAdapter(program.binding, client)
        self.program, self.gateway = program, gateway
        self.client = client
        self.checkpointer = checkpointer or InMemorySaver(serde=LangGraphAGJSONSerde())
        self._config: RunnableConfig = {
            "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
            "callbacks": [],
            "max_concurrency": 1,
            "recursion_limit": 16,
        }
        builder = StateGraph(LangGraphValues)
        builder.add_node("fetch", self._fetch)
        builder.add_node("review", self._review)
        builder.add_node("publish", self._publish)
        builder.add_edge(START, "fetch")
        builder.add_conditional_edges(
            "fetch",
            lambda state: END if state["error_code"] else "review",
            {END: END, "review": "review"},
        )
        builder.add_edge("review", "publish")
        builder.add_edge("publish", END)
        self._graph = builder.compile(
            checkpointer=self.checkpointer,
            interrupt_before=list(program.graph.pause_before),
            interrupt_after=["fetch", "review"] if step_boundaries else [],
        )

    def invoke(self, *, start: bool = False) -> dict[str, Any]:
        with tracing_context(enabled=False):
            current = self.checkpointer.get_tuple(self._config)
            if start and current is not None:
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "LangGraph thread 已初始化，不能重复开始"
                )
            if not start and current is None:
                raise AgentGitError(
                    ErrorCode.RECORDING_MISSING, "LangGraph thread 没有可恢复的 checkpoint"
                )
            result = self._graph.invoke(
                initial_values(self.program) if start else None,
                self._config,
                durability="sync",
            )
        return dict(result)

    def state(self) -> StateSnapshot:
        with tracing_context(enabled=False):
            return self._graph.get_state(self._config)

    def update_note(self, note: str) -> None:
        # 调用者须先在独立目标 thread 还原安全 checkpoint，不能就地修改来源 Run。
        if len(note) > 2048 or self.state().next != ("review",):
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "LangGraph 仅支持 review 前的有界备注更新"
            )
        with tracing_context(enabled=False):
            self._graph.update_state(self._config, {"review_note": note}, as_node="fetch")

    def _fetch(self, state: LangGraphValues) -> dict[str, Any]:
        outcome = MCPAdapter(self.program.binding, self.client).call(
            self.gateway, step="fetch", attempt=1, arguments=self.program.request.arguments
        )
        order = [*state["node_order"], "fetch"]
        if outcome.error is not None:
            return {"error_code": outcome.error.code.value, "node_order": order}
        if len(outcome.payload) > self.program.binding.server.max_response_bytes:
            return {"error_code": ErrorCode.OUTPUT_LIMIT.value, "node_order": order}
        value = decode(outcome.payload)
        binding = self.program.binding
        request_ref = object_digest(
            ObjectType.BLOB, encode(self.program.request.model_dump(mode="json"))
        )
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("kind") != "mcp_tool_result/v1"
            or not isinstance(value.get("content"), list)
            or value.get("server_identity") != binding.server.server_identity
            or value.get("tool_name") != binding.tool_name
            or value.get("schema_digest") != binding.schema_digest()
            or value.get("arguments_digest") != request_ref
            or value.get("effect_class") != "READ_ONLY"
            or value.get("cancellation") != "transport_only"
            or outcome.trace.logical_step_id != "fetch"
            or outcome.trace.call_index != 0
            or outcome.trace.attempt != 1
            or outcome.trace.error_code is not None
            or outcome.trace.provider != binding.provider
            or outcome.trace.signature_digest != binding.signature()
            or outcome.trace.request_ref != request_ref
            or outcome.trace.response_ref != object_digest(ObjectType.BLOB, outcome.payload)
        ):
            return {"error_code": ErrorCode.REPLAY_MISMATCH.value, "node_order": order}
        content = value["content"]
        if any(
            not isinstance(item, dict)
            or set(item) != {"type", "text"}
            or item.get("type") != "text"
            or not isinstance(item.get("text"), str)
            for item in content
        ):
            return {"error_code": ErrorCode.REPLAY_MISMATCH.value, "node_order": order}
        text = "\n".join(item["text"] for item in content)
        if len(text.encode("utf-8")) > self.program.limits.max_context_bytes:
            return {"error_code": ErrorCode.OUTPUT_LIMIT.value, "node_order": order}
        return {"fetch_ref": outcome.trace.response_ref, "fetch_text": text, "node_order": order}

    def _review(self, state: LangGraphValues) -> dict[str, Any]:
        text = state["fetch_text"]
        if state["review_note"]:
            text += "\n\n审阅备注：" + state["review_note"]
        if len(text.encode("utf-8")) > self.program.limits.max_context_bytes:
            return {
                "error_code": ErrorCode.OUTPUT_LIMIT.value,
                "node_order": [*state["node_order"], "review"],
            }
        return {"reviewed_text": text, "node_order": [*state["node_order"], "review"]}

    def _publish(self, state: LangGraphValues) -> dict[str, Any]:
        if state["error_code"]:
            return {"node_order": [*state["node_order"], "publish"]}
        report = "# 取证报告\n\n" + state["reviewed_text"] + "\n"
        if len(report.encode("utf-8")) > self.program.limits.max_artifact_bytes:
            return {
                "error_code": ErrorCode.OUTPUT_LIMIT.value,
                "node_order": [*state["node_order"], "publish"],
            }
        return {"report": report, "node_order": [*state["node_order"], "publish"]}
