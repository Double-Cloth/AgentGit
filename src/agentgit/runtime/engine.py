"""Plan → Act → Observe；所有完成的状态转移均经过 StateJournal。"""

import hashlib
import time
from collections.abc import Callable

from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass, RunStatus, parse_contract
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    Evidence,
    FinalDecision,
    ModelRequest,
    PlanDecision,
    Source,
    ToolDecision,
    evolve,
    parse_decision,
)
from agentgit.runtime.ports import CallGateway, ModelAdapter, StateJournal
from agentgit.runtime.validation import validate_report


class ResearchRuntime:
    def __init__(
        self,
        state: AgentState,
        config: AgentConfig,
        model: ModelAdapter,
        tools: ToolRegistry,
        gateway: CallGateway,
        journal: StateJournal,
        *,
        clock_ms: Callable[[], int] | None = None,
    ):
        self.state, self.config, self.model, self.tools = state, config, model, tools
        self.gateway, self.journal = gateway, journal
        self.clock_ms = clock_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self.started_at = self.clock_ms()

    def _save(self, event_type: str, **details: object) -> None:
        self.journal.save(self.state, event_type, dict(details))

    def _wall(self) -> None:
        if self.clock_ms() - self.started_at >= self.config.limits.max_wall_ms:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "Run 墙钟预算已耗尽")

    def run(self) -> AgentState:
        if self.state.status == RunStatus.QUEUED:
            self.state = evolve(self.state, status=RunStatus.PLANNING)
            self._save("run.planning")
        while self.state.status in (RunStatus.PLANNING, RunStatus.RUNNING):
            try:
                self._wall()
                if self.state.pending_tool is not None:
                    self._tool(self.state.pending_tool)
                else:
                    self._model()
            except AgentGitError as exc:
                if exc.code in (ErrorCode.VERSION_CONFLICT, ErrorCode.OBJECT_CORRUPT):
                    raise
                self._failure(exc)
        return self.state

    def _model(self) -> None:
        state, limits = self.state, self.config.limits
        request = encode(
            ModelRequest(
                prompt=self.config.prompt,
                phase="plan" if state.status == RunStatus.PLANNING else "act",
                state=state,
                evaluation_time=self.tools.corpus.evaluation_time,
            ).model_dump(mode="json")
        )
        if len(request) > limits.max_context_bytes:
            raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "上下文超过上限，未进行有损摘要")
        reserve = len(request) + limits.max_response_bytes
        usage = state.usage
        if (
            usage.steps >= limits.max_steps
            or usage.model_calls >= limits.max_model_calls
            or usage.tokens + reserve > limits.max_tokens
        ):
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "模型步骤、调用或 Token 预留预算不足")
        self.state = evolve(
            state,
            usage=evolve(
                usage,
                steps=usage.steps + 1,
                model_calls=usage.model_calls + 1,
                reserved_tokens=reserve,
            ),
        )
        outcome = self.gateway.call(
            step=f"model-{self.state.usage.steps}",
            attempt=1,
            provider="llm:mock-research/v1",
            signature=self.model.signature,
            effect=EffectClass.READ_ONLY,
            request=request,
            invoke=lambda: self.model.invoke(request),
            max_bytes=limits.max_response_bytes,
        )
        self.state = evolve(
            self.state,
            usage=evolve(
                self.state.usage,
                reserved_tokens=0,
                tokens=usage.tokens + len(request) + outcome.output_bytes,
            ),
            messages=(*self.state.messages, outcome.trace),
        )
        if outcome.error:
            raise outcome.error
        self._wall()
        if not outcome.payload:
            raise AgentGitError(ErrorCode.MODEL_EMPTY, "模型返回空响应")
        try:
            decision = parse_decision(outcome.payload)
        except AgentGitError as exc:
            if self.state.status == RunStatus.PLANNING:
                raise AgentGitError(
                    ErrorCode.PLAN_INVALID, "模型计划不符合 Schema 或 DAG 约束"
                ) from exc
            raise
        if isinstance(decision, PlanDecision):
            if self.state.status != RunStatus.PLANNING:
                raise AgentGitError(ErrorCode.PLAN_INVALID, "非规划阶段不能替换计划")
            if self.state.plan is not None:
                old = {node.key: node for node in self.state.plan.nodes}
                new = {node.key: node for node in decision.plan.nodes}
                if any(key not in new or old[key] != new[key] for key in self.state.completed):
                    raise AgentGitError(ErrorCode.PLAN_INVALID, "重规划不能改写已完成节点")
            self.state = evolve(
                self.state,
                plan=decision.plan,
                plan_version=self.state.plan_version + 1,
                status=RunStatus.RUNNING,
                error_code=None,
            )
            self._save("plan.accepted", plan_version=self.state.plan_version)
        elif isinstance(decision, ToolDecision):
            self._decision(decision)
        elif isinstance(decision, FinalDecision):
            if self.state.plan is None or len(self.state.completed) != len(self.state.plan.nodes):
                raise AgentGitError(ErrorCode.PLAN_INVALID, "尚未完成计划不能结束任务")
            evaluation = validate_report(
                self.state, self.tools.workspace.contents(), self.tools.corpus
            )
            self.state = evolve(
                self.state,
                evaluation=evaluation,
                status=RunStatus.COMPLETED if evaluation.passed else RunStatus.FAILED,
                error_code=None if evaluation.passed else ErrorCode.VALIDATION_FAILED.value,
            )
            self._save("run.evaluated", evaluation=evaluation.model_dump(mode="json"))

    def _decision(self, decision: ToolDecision) -> None:
        fingerprint = object_digest(ObjectType.CALL, encode(decision.model_dump(mode="json")))
        repeated = self.state.repeated_actions + 1 if self.state.last_action == fingerprint else 1
        self.state = evolve(self.state, last_action=fingerprint, repeated_actions=repeated)
        if repeated >= 3:
            raise AgentGitError(ErrorCode.LOOP_DETECTED, "重复无进展决策已达到停止阈值")
        self._validate_tool_step(decision)
        self.tools.effect(decision.tool)
        self.state = evolve(self.state, pending_tool=decision)
        self._save("decision.accepted", node=decision.node_key)

    def _validate_tool_step(self, decision: ToolDecision) -> None:
        if self.state.status != RunStatus.RUNNING or self.state.plan is None:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "执行决策必须处于有效计划中")
        ready = {node.key: node for node in self.state.plan.ready(self.state.completed)}
        if decision.node_key not in ready:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "节点不存在、已完成或前置依赖未满足")
        expected = {
            "search": "corpus_search",
            "read": "source_read",
            "write_report": "artifact_write",
            "write_sources": "artifact_write",
        }
        if decision.tool != expected[ready[decision.node_key].kind]:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "工具与计划节点类型不一致")

    def _tool(self, decision: ToolDecision) -> None:
        self._validate_tool_step(decision)
        usage = self.state.usage
        if usage.tool_calls >= self.config.limits.max_tool_calls:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "工具调用预算已耗尽")
        attempt = self.state.attempts.get(decision.node_key, 0) + 1
        self.state = evolve(
            self.state,
            usage=evolve(usage, tool_calls=usage.tool_calls + 1),
            attempts={**self.state.attempts, decision.node_key: attempt},
        )
        outcome = self.gateway.call(
            step=decision.node_key,
            attempt=attempt,
            provider=f"tool:{decision.tool}/v1",
            signature=self.tools.signature(decision.tool),
            effect=self.tools.effect(decision.tool),
            request=encode(decision.arguments),
            invoke=lambda: self.tools.invoke(decision.tool, decision.arguments),
            max_bytes=self.config.limits.max_response_bytes,
        )
        self.state = evolve(self.state, messages=(*self.state.messages, outcome.trace))
        if outcome.error:
            raise outcome.error
        self._wall()
        if not outcome.payload:
            raise AgentGitError(ErrorCode.TOOL_EMPTY, "工具返回空响应")
        result = decode(outcome.payload)
        if (
            not isinstance(result, dict)
            or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "工具响应必须符合已声明的 Schema")
        if decision.tool == "corpus_search":
            ids = result.get("source_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or any(type(item) is not str for item in ids)
                or len(set(ids)) != len(ids)
                or set(result) != {"schema_version", "source_ids"}
            ):
                raise AgentGitError(ErrorCode.TOOL_EMPTY, "搜索未返回合法来源列表")
            self.state = evolve(self.state, search_results=tuple(ids))
        elif decision.tool == "source_read":
            if set(result) != {"schema_version", "source"}:
                raise AgentGitError(ErrorCode.VALIDATION, "来源响应含未知字段")
            source = parse_contract(encode(result["source"]), Source)
            if (
                source.id != decision.arguments.get("source_id")
                or source.id not in self.state.search_results
                or hashlib.sha256(source.body.encode()).hexdigest() != source.body_sha256
            ):
                raise AgentGitError(ErrorCode.VALIDATION, "来源身份或正文摘要不符")
            memory = {
                **self.state.working_memory,
                source.id: Evidence(
                    source=source, logical_step_id=decision.node_key, attempt=attempt
                ),
            }
            self.state = evolve(self.state, working_memory=memory)
        else:
            if set(result) != {"schema_version", "path", "content_ref"} or result[
                "path"
            ] != decision.arguments.get("path"):
                raise AgentGitError(ErrorCode.VALIDATION, "产物响应不符合 Schema")
            content = self.tools.workspace.contents().get(str(result["path"]))
            if content is None or object_digest(ObjectType.BLOB, content) != result["content_ref"]:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "产物响应与工作区内容不符")
        self.state = evolve(
            self.state,
            completed=(*self.state.completed, decision.node_key),
            pending_tool=None,
            error_code=None,
        )
        self._save("tool.observed", node=decision.node_key)

    def _failure(self, error: AgentGitError) -> None:
        retryable = {
            ErrorCode.VALIDATION,
            ErrorCode.PLAN_INVALID,
            ErrorCode.MODEL_EMPTY,
            ErrorCode.MODEL_ERROR,
            ErrorCode.TOOL_EMPTY,
            ErrorCode.TOOL_ERROR,
            ErrorCode.NOT_FOUND,
        }
        usage = self.state.usage
        if error.code in retryable and usage.replans < self.config.limits.max_replans:
            self.state = evolve(
                self.state,
                status=RunStatus.PLANNING,
                pending_tool=None,
                error_code=error.code.value,
                usage=evolve(usage, replans=usage.replans + 1, reserved_tokens=0),
            )
            self._save("run.replanning", error=error.as_dict())
            return
        status = (
            RunStatus.BUDGET_EXHAUSTED
            if error.code == ErrorCode.BUDGET_EXHAUSTED
            else RunStatus.OUTCOME_UNKNOWN
            if error.code == ErrorCode.OUTCOME_UNKNOWN
            else RunStatus.FAILED
        )
        self.state = evolve(
            self.state,
            status=status,
            pending_tool=self.state.pending_tool if status == RunStatus.OUTCOME_UNKNOWN else None,
            error_code=error.code.value,
            usage=evolve(
                usage,
                reserved_tokens=usage.reserved_tokens if status == RunStatus.OUTCOME_UNKNOWN else 0,
            ),
        )
        self._save("run.stopped", error=error.as_dict())
