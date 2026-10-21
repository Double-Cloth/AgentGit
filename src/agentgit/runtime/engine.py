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
    BudgetUsage,
    Evidence,
    FinalDecision,
    ModelRequest,
    PlanDecision,
    Source,
    ToolDecision,
    evolve,
    parse_decision,
)
from agentgit.runtime.ports import (
    CallGateway,
    CallOutcome,
    CallSettlement,
    ModelAdapter,
    StateJournal,
)
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
        control: Callable[[], str | None] | None = None,
        wall_budget_ms: int | None = None,
        settlement: CallSettlement | None = None,
        budget_baseline: BudgetUsage | None = None,
    ):
        self.state, self.config, self.model, self.tools = state, config, model, tools
        self.gateway, self.journal = gateway, journal
        self.clock_ms = clock_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self.started_at = self.clock_ms()
        self.control, self.wall_budget_ms = control, wall_budget_ms
        self.settlement = settlement
        self.budget_baseline = budget_baseline or BudgetUsage()
        if any(
            getattr(state.usage, name) < getattr(self.budget_baseline, name)
            for name in (
                "steps",
                "model_calls",
                "tool_calls",
                "tokens",
                "replans",
                "cost_micro_units",
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "预算基线不能超过当前逻辑用量")

    def _save(self, event_type: str, **details: object) -> None:
        self.journal.save(self.state, event_type, dict(details))

    def _wall(self) -> None:
        limit = self.config.limits.max_wall_ms
        if self.wall_budget_ms is not None:
            limit = min(limit, self.wall_budget_ms)
        if self.clock_ms() - self.started_at >= limit:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "Run 墙钟预算已耗尽")

    def run(self) -> AgentState:
        if self.settlement is not None:
            try:
                self._settle(self.settlement)
            except AgentGitError as exc:
                if exc.code in (
                    ErrorCode.REPLAY_MISMATCH,
                    ErrorCode.OBJECT_CORRUPT,
                    ErrorCode.VERSION_CONFLICT,
                ):
                    raise
                self._failure(exc)
            if self.state.status not in (RunStatus.PLANNING, RunStatus.RUNNING):
                return self.state
        if self.state.status in (RunStatus.PLANNING, RunStatus.RUNNING):
            try:
                self._consume_recorded()
            except AgentGitError as exc:
                if exc.code in (
                    ErrorCode.VERSION_CONFLICT,
                    ErrorCode.OBJECT_CORRUPT,
                    ErrorCode.REPLAY_MISMATCH,
                ):
                    raise
                if not self._control_point(frozenset({"cancel", "budget_exhausted"})):
                    self._failure(exc)
            if self.state.status not in (RunStatus.PLANNING, RunStatus.RUNNING):
                return self.state
        if self._control_point():
            return self.state
        if self.state.status == RunStatus.QUEUED:
            self.state = evolve(self.state, status=RunStatus.PLANNING)
            self._save("run.planning")
        while self.state.status in (RunStatus.PLANNING, RunStatus.RUNNING):
            before_call = self.state
            try:
                if self._control_point():
                    break
                self._wall()
                if self.state.pending_tool is not None:
                    self._tool(self.state.pending_tool)
                else:
                    self._model()
            except AgentGitError as exc:
                if exc.code == ErrorCode.CONTROL_REQUESTED:
                    # 尚无调用意图或实际发送，撤回本轮内存中的计数/预留。
                    self.state = before_call
                    if self.control is None or not self._control_point():
                        raise AgentGitError(
                            ErrorCode.VERSION_CONFLICT, "控制请求与执行上下文不符"
                        ) from exc
                    continue
                if exc.code in (
                    ErrorCode.VERSION_CONFLICT,
                    ErrorCode.OBJECT_CORRUPT,
                    ErrorCode.APPROVAL_REQUIRED,
                ):
                    raise
                self._failure(exc)
        return self.state

    def _control_point(self, only: frozenset[str] | None = None) -> bool:
        action = self.control() if self.control is not None else None
        if only is not None and action not in only:
            return False
        if action == "pause":
            resume: RunStatus = (
                RunStatus.PLANNING if self.state.status == RunStatus.QUEUED else self.state.status
            )
            self.state = evolve(self.state, status=RunStatus.PAUSED, resume_status=resume)
            self._save("run.paused")
            return True
        if action == "cancel":
            self.state = evolve(
                self.state, status=RunStatus.CANCELLED, pending_tool=None, resume_status=None
            )
            self._save("run.cancelled")
            return True
        if action == "resume" and self.state.status == RunStatus.PAUSED:
            resume = self.state.resume_status or (
                RunStatus.RUNNING if self.state.plan else RunStatus.PLANNING
            )
            self.state = evolve(self.state, status=resume, resume_status=None)
            self._save("run.resumed")
        if action == "budget_exhausted":
            self.state = evolve(
                self.state,
                status=RunStatus.BUDGET_EXHAUSTED,
                error_code=ErrorCode.BUDGET_EXHAUSTED.value,
            )
            self._save(
                "run.stopped", error={"code": "budget_exhausted", "message": "持久化墙钟期限已到"}
            )
            return True
        return False

    def _model_request(self) -> bytes:
        return encode(
            ModelRequest(
                prompt=self.config.prompt,
                phase="plan" if self.state.status == RunStatus.PLANNING else "act",
                state=self.state,
                evaluation_time=self.tools.corpus.evaluation_time,
                available_tools=self.config.tools,
                execution_context=self.tools.context(),
            ).model_dump(mode="json")
        )

    def _consume_recorded(self) -> None:
        while self.state.status in (RunStatus.PLANNING, RunStatus.RUNNING):
            decision = self.state.pending_tool
            if decision is not None:
                self._validate_tool_step(decision)
                attempt = self.state.attempts.get(decision.node_key, 0) + 1
                outcome = self.gateway.recorded(
                    step=decision.node_key,
                    attempt=attempt,
                    provider=f"tool:{decision.tool}/v1",
                    signature=self.tools.signature(decision.tool),
                    effect=self.tools.effect(decision.tool),
                    request=encode(decision.arguments),
                    max_bytes=self.config.limits.max_response_bytes,
                )
                if outcome is None:
                    return
                self.state = evolve(
                    self.state,
                    usage=evolve(self.state.usage, tool_calls=self.state.usage.tool_calls + 1),
                    attempts={**self.state.attempts, decision.node_key: attempt},
                )
                if outcome.error is None:
                    self.tools.prepare(decision.tool, decision.arguments)
                self._observe_tool(decision, attempt, outcome, settling=True)
            else:
                request = self._model_request()
                outcome = self.gateway.recorded(
                    step=f"model-{self.state.usage.steps + 1}",
                    attempt=1,
                    provider="llm:mock-research/v1",
                    signature=self.model.signature,
                    effect=EffectClass.READ_ONLY,
                    request=request,
                    max_bytes=self.config.limits.max_response_bytes,
                )
                if outcome is None:
                    return
                usage = self.state.usage
                self.state = evolve(
                    self.state,
                    usage=evolve(
                        usage,
                        steps=usage.steps + 1,
                        model_calls=usage.model_calls + 1,
                        reserved_tokens=len(request) + self.config.limits.max_response_bytes,
                    ),
                )
                self._observe_model(outcome, len(request), settling=True)

    def _model(self) -> None:
        state, limits = self.state, self.config.limits
        request = self._model_request()
        if len(request) > limits.max_context_bytes:
            raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "上下文超过上限，未进行有损摘要")
        reserve = len(request) + limits.max_response_bytes
        usage = state.usage
        if (
            usage.steps - self.budget_baseline.steps >= limits.max_steps
            or usage.model_calls - self.budget_baseline.model_calls >= limits.max_model_calls
            or usage.tokens - self.budget_baseline.tokens + reserve > limits.max_tokens
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
        self._observe_model(outcome, len(request))

    def _observe_model(
        self, outcome: CallOutcome, request_bytes: int, *, settling: bool = False
    ) -> None:
        if outcome.error is not None and outcome.error.code == ErrorCode.OUTCOME_UNKNOWN:
            self.state = evolve(self.state, messages=(*self.state.messages, outcome.trace))
            raise outcome.error
        self.state = evolve(
            self.state,
            usage=evolve(
                self.state.usage,
                reserved_tokens=0,
                tokens=self.state.usage.tokens + request_bytes + outcome.output_bytes,
            ),
            messages=(*self.state.messages, outcome.trace),
        )
        if outcome.error:
            raise outcome.error
        if not settling:
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
            if settling and self._control_point(frozenset({"cancel", "budget_exhausted"})):
                return
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
            "write_execution_code": "artifact_write",
            "write_execution_command": "artifact_write",
            "code_exec": "code_exec",
            "terminal_exec": "terminal_exec",
        }
        if decision.tool != expected[ready[decision.node_key].kind]:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "工具与计划节点类型不一致")

    def _tool(self, decision: ToolDecision) -> None:
        self._validate_tool_step(decision)
        self.tools.prepare(decision.tool, decision.arguments)
        usage = self.state.usage
        if usage.tool_calls - self.budget_baseline.tool_calls >= self.config.limits.max_tool_calls:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "工具调用预算已耗尽")
        attempt = self.state.attempts.get(decision.node_key, 0) + 1
        if (
            decision.tool in {"code_exec", "terminal_exec"}
            and self.tools.policy is not None
            and attempt > self.tools.policy.max_attempts
        ):
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "容器工具已达到固定尝试次数上限")
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
        self._observe_tool(decision, attempt, outcome)

    def _observe_tool(
        self, decision: ToolDecision, attempt: int, outcome: CallOutcome, *, settling: bool = False
    ) -> None:
        self.state = evolve(self.state, messages=(*self.state.messages, outcome.trace))
        if outcome.error:
            if outcome.error.code in {
                ErrorCode.EXECUTION_CANCELLED,
                ErrorCode.EXECUTION_TIMEOUT,
            } and self._control_point(frozenset({"cancel", "budget_exhausted"})):
                return
            raise outcome.error
        if not settling:
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
        elif decision.tool in {"code_exec", "terminal_exec"}:
            self.tools.observe_execution(
                decision.tool, decision.arguments, outcome.payload, outcome.trace
            )
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

    def _settle(self, settlement: CallSettlement) -> None:
        unknown, before, trace = self.state, settlement.before, settlement.outcome.trace
        if unknown.status != RunStatus.OUTCOME_UNKNOWN or before.status not in (
            RunStatus.PLANNING,
            RunStatus.RUNNING,
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "核对状态没有对应的未决调用")
        normalized = evolve(
            unknown,
            status=before.status,
            error_code=before.error_code,
            usage=before.usage,
            messages=before.messages,
            attempts=before.attempts,
            resume_status=before.resume_status,
        )
        if normalized != before or unknown.messages[: len(before.messages)] != before.messages:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知状态与调用前检查点不符")
        extra = unknown.messages[len(before.messages) :]
        if extra and (
            len(extra) != 1
            or extra[0].error_code != ErrorCode.OUTCOME_UNKNOWN.value
            or extra[0].model_dump(exclude={"response_ref", "error_code"})
            != trace.model_dump(exclude={"response_ref", "error_code"})
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "未知调用轨迹与核对结果不符")
        if trace.request_ref != object_digest(ObjectType.BLOB, settlement.request):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "核对请求摘要不符")
        model = trace.provider.startswith("llm:")
        expected_usage = evolve(
            before.usage,
            steps=before.usage.steps + int(model),
            model_calls=before.usage.model_calls + int(model),
            tool_calls=before.usage.tool_calls + int(not model),
            reserved_tokens=len(settlement.request) + self.config.limits.max_response_bytes
            if model
            else before.usage.reserved_tokens,
        )
        if unknown.usage != expected_usage:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "核对不能重置或重复扣除预算")
        if model:
            request = parse_contract(settlement.request, ModelRequest)
            if (
                request.state != before
                or request.prompt != self.config.prompt
                or request.evaluation_time != self.tools.corpus.evaluation_time
                or request.phase != ("plan" if before.status == RunStatus.PLANNING else "act")
                or trace.logical_step_id != f"model-{unknown.usage.steps}"
                or trace.attempt != 1
                or trace.signature_digest != self.model.signature
                or unknown.attempts != before.attempts
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "模型核对身份不符")
        else:
            decision = before.pending_tool
            if (
                decision is None
                or trace.provider != f"tool:{decision.tool}/v1"
                or trace.logical_step_id != decision.node_key
                or trace.attempt != before.attempts.get(decision.node_key, 0) + 1
                or trace.signature_digest != self.tools.signature(decision.tool)
                or settlement.request != encode(decision.arguments)
                or unknown.attempts != {**before.attempts, decision.node_key: trace.attempt}
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "工具核对身份不符")
        self.state = evolve(before, usage=unknown.usage, attempts=unknown.attempts, error_code=None)
        if model:
            self._observe_model(settlement.outcome, len(settlement.request), settling=True)
        else:
            assert decision is not None
            self._validate_tool_step(decision)
            self._observe_tool(decision, trace.attempt, settlement.outcome, settling=True)

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
        if (
            error.code in retryable
            and usage.replans - self.budget_baseline.replans < self.config.limits.max_replans
        ):
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
            resume_status=self.state.status if status == RunStatus.OUTCOME_UNKNOWN else None,
            pending_tool=self.state.pending_tool if status == RunStatus.OUTCOME_UNKNOWN else None,
            error_code=error.code.value,
            usage=evolve(
                usage,
                reserved_tokens=usage.reserved_tokens if status == RunStatus.OUTCOME_UNKNOWN else 0,
            ),
        )
        self._save("run.stopped", error=error.as_dict())
