"""将显式干预应用到来源副本；权限、预算及工作区由新 Job 独立建立。"""

from collections.abc import Callable
from dataclasses import dataclass

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import (
    CodeArguments,
    ExecutionFile,
    ExecutionManifest,
    TerminalArguments,
)
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage, ToolDecision, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import LiveOverrides
from agentgit.replay.workspace import MemoryWorkspace


@dataclass(frozen=True)
class PreparedLive:
    snapshot: StateSnapshot
    files: dict[str, bytes]
    budget_baseline: BudgetUsage


def prepare_live(
    snapshot: StateSnapshot,
    config: AgentConfig,
    overrides: LiveOverrides,
    read: Callable[[str, ObjectType], bytes],
) -> PreparedLive:
    original = parse_contract(encode(snapshot.data), AgentState)
    if original.status in (
        RunStatus.OUTCOME_UNKNOWN,
        RunStatus.WAITING_APPROVAL,
        RunStatus.INTERRUPTED,
    ):
        raise AgentGitError(ErrorCode.FORBIDDEN, "Live 只能从安全检查点开始")
    files: dict[str, bytes] = {}
    total = 0
    for item in snapshot.artifacts:
        payload = read(item.object_ref, ObjectType.BLOB)
        total += len(payload)
        if total > 64 * 1024 * 1024:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Live 来源工作区超过 64 MiB")
        files[item.path] = payload
    media = {item.path: item.media_type for item in snapshot.artifacts}
    for patch in overrides.artifacts:
        if patch.operation == "delete":
            if patch.path not in files:
                raise AgentGitError(ErrorCode.VALIDATION, "删除目标不在来源工作区")
            del files[patch.path]
            del media[patch.path]
        else:
            files[patch.path] = patch.content()
            media[patch.path] = patch.media_type or media.get(patch.path, "text/plain")
    workspace = MemoryWorkspace(
        max_file_bytes=config.limits.max_artifact_bytes, max_files=config.limits.max_files
    )
    workspace.replace(files, media)
    plan = overrides.plan or original.plan
    permissions = {
        "search": "corpus_search",
        "read": "source_read",
        "write_report": "artifact_write",
        "write_sources": "artifact_write",
        "write_execution_code": "artifact_write",
        "write_execution_command": "artifact_write",
        "code_exec": "code_exec",
        "terminal_exec": "terminal_exec",
    }
    required = (
        {permissions[node.kind] for node in plan.nodes if node.key not in original.completed}
        if plan is not None
        else {"corpus_search", "source_read", "artifact_write"}
    )
    if required - set(config.tools):
        raise AgentGitError(ErrorCode.FORBIDDEN, "剩余计划需要的工具未获新配置授权")
    if overrides.plan is not None and original.plan is not None:
        before, after = (
            {node.key: node for node in original.plan.nodes},
            {node.key: node for node in overrides.plan.nodes},
        )
        if any(key not in after or after[key] != before[key] for key in original.completed):
            raise AgentGitError(ErrorCode.PLAN_INVALID, "Live 不能改写已完成的计划节点")
    pending = original.pending_tool
    if overrides.config is not None or overrides.plan is not None:
        pending = None
    if (
        pending is not None
        and pending.tool == "artifact_write"
        and any(patch.path == pending.arguments.get("path") for patch in overrides.artifacts)
    ):
        pending = None
    if config.execution_policy is not None and sum(map(len, files.values())) > 32 * 1024 * 1024:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Live 执行文件总量超过 32 MiB")
    manifest = ExecutionManifest(
        files=tuple(
            ExecutionFile(
                path=path,
                object_ref=object_digest(ObjectType.BLOB, payload),
                media_type=media[path],
                byte_length=len(payload),
            )
            for path, payload in sorted(files.items())
            if config.execution_policy is not None
        )
    )
    manifest_ref = object_digest(
        ObjectType.ARTIFACT_MANIFEST, encode(manifest.model_dump(mode="json"))
    )

    def reference(path: str) -> str:
        if path not in files:
            raise AgentGitError(ErrorCode.VALIDATION, "执行参数引用的文件不在新工作区")
        return object_digest(ObjectType.BLOB, files[path])

    def rebound(value: str | None) -> str | None:
        if value is None:
            return None
        candidates = {
            reference(item.path)
            for item in snapshot.artifacts
            if item.object_ref == value and item.path in files
        }
        if len(candidates) != 1:
            raise AgentGitError(
                ErrorCode.VALIDATION, "执行引用无法唯一绑定新文件，请明确选择执行文件"
            )
        return next(iter(candidates))

    execution = overrides.execution
    if execution is not None:
        if plan is None:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "执行干预需要有效计划")
        candidates = [
            node
            for node in plan.ready(original.completed)
            if node.kind == execution.tool
            and (execution.node_key is None or node.key == execution.node_key)
        ]
        if len(candidates) != 1:
            raise AgentGitError(ErrorCode.PLAN_INVALID, "执行干预必须精确匹配一个就绪节点")
        if config.execution_policy is None or execution.tool not in config.tools:
            raise AgentGitError(ErrorCode.FORBIDDEN, "新配置未授权执行工具")
        stdin = reference(execution.stdin_path) if execution.stdin_path is not None else None
        arguments: TerminalArguments | CodeArguments
        if execution.tool == "terminal_exec":
            arguments = TerminalArguments(
                command_ref=reference(execution.path),
                input_manifest_ref=manifest_ref,
                execution_policy_ref=config.execution_policy.digest(),
                cwd=execution.cwd,
                timeout_ms=execution.timeout_ms,
                stdin_ref=stdin,
            )
        else:
            reference(execution.path)
            arguments = CodeArguments(
                script_path=execution.path,
                argv=execution.argv,
                input_manifest_ref=manifest_ref,
                execution_policy_ref=config.execution_policy.digest(),
                cwd=execution.cwd,
                timeout_ms=execution.timeout_ms,
                stdin_ref=stdin,
            )
        pending = ToolDecision(
            node_key=candidates[0].key,
            tool=execution.tool,
            arguments=arguments.model_dump(mode="json"),
        )
    elif pending is not None and pending.tool in ("code_exec", "terminal_exec"):
        if config.execution_policy is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "新配置未授权执行工具")
        updated: TerminalArguments | CodeArguments
        if pending.tool == "terminal_exec":
            terminal = parse_contract(encode(pending.arguments), TerminalArguments)
            updated = evolve(
                terminal,
                command_ref=rebound(terminal.command_ref),
                stdin_ref=rebound(terminal.stdin_ref),
                input_manifest_ref=manifest_ref,
                execution_policy_ref=config.execution_policy.digest(),
            )
        else:
            code = parse_contract(encode(pending.arguments), CodeArguments)
            if code.script_path is not None:
                reference(code.script_path)
            updated = evolve(
                code,
                code_ref=rebound(code.code_ref),
                stdin_ref=rebound(code.stdin_ref),
                input_manifest_ref=manifest_ref,
                execution_policy_ref=config.execution_policy.digest(),
            )
        pending = evolve(pending, arguments=updated.model_dump(mode="json"))
    if pending is not None and pending.tool == "terminal_exec":
        command = parse_contract(encode(pending.arguments), TerminalArguments)
        by_ref = {object_digest(ObjectType.BLOB, content): content for content in files.values()}
        if len(by_ref[command.command_ref]) > 16384:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Live 终端命令超过 16 KiB")
    if pending is not None and pending.tool not in config.tools:
        raise AgentGitError(ErrorCode.FORBIDDEN, "待执行决策未获新配置授权")
    state = evolve(
        original,
        plan=plan,
        plan_version=original.plan_version + int(overrides.plan is not None),
        status=RunStatus.RUNNING if plan is not None else RunStatus.PLANNING,
        usage=evolve(original.usage, reserved_tokens=0),
        pending_tool=pending,
        attempts={
            key: value for key, value in original.attempts.items() if key in original.completed
        },
        messages=tuple(
            trace
            for trace in original.messages
            if trace.logical_step_id in original.completed or trace.provider.startswith("llm:")
        ),
        stopped_executions=(),
        last_action=None,
        repeated_actions=0,
        error_code=None,
        evaluation=None,
        resume_status=None,
    )
    return PreparedLive(workspace.snapshot(state), workspace.contents(), state.usage)
