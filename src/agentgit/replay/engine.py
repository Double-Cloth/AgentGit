"""用原 Runtime 重算逻辑状态；来源快照仅作起点和逐步比较证据。"""

import hashlib
import platform
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentgit import __version__
from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import ToolRegistry, WriteArguments
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import (
    ExecutionContext,
    ExecutionFile,
    ExecutionManifest,
    ExecutionResult,
)
from agentgit.core.models import CommitManifest, EffectClass, RunStatus, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage, CallTrace
from agentgit.core.snapshots import EventManifest, EventPayload, StateSnapshot
from agentgit.replay.recorded import RecordedAdapter
from agentgit.replay.recovery import rebuild_discard
from agentgit.replay.workspace import MemoryWorkspace
from agentgit.runtime.engine import ResearchRuntime
from agentgit.runtime.ports import CallOutcome
from agentgit.services.environment import source_revision

if TYPE_CHECKING:
    from agentgit.services.recordings import RecordedBundle

ObjectReader = Callable[[str, ObjectType], bytes]


@dataclass(frozen=True)
class RebuiltCheckpoint:
    source_commit: str
    state_ref: str
    snapshot: StateSnapshot
    event_type: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ReplayResult:
    restored_from: str
    checkpoints: tuple[RebuiltCheckpoint, ...]
    state: AgentState
    consumed_calls: int
    actual_model_calls: int = 0
    actual_tool_calls: int = 0


class _ReachedUntil(Exception):
    pass


class _ReplayAbort(Exception):
    def __init__(self, error: AgentGitError):
        self.error = error


class _RecordedModel:
    def __init__(self, signature: str):
        self.signature = signature

    def invoke(self, payload: bytes) -> bytes:
        raise AssertionError("Recorded 不能调用模型")


class _Gateway:
    def __init__(self, adapter: RecordedAdapter, workspace: MemoryWorkspace):
        self.adapter, self.workspace = adapter, workspace

    def recorded(self, **kwargs: Any) -> CallOutcome | None:
        if (
            self.adapter.position == len(self.adapter.recordings)
            or not self.adapter.recordings[self.adapter.position].reused
        ):
            return None

        def forbidden() -> bytes:
            raise AssertionError("Recorded 不能调用真实 Adapter")

        # 来源恢复先结算已保存结果，再处理取消/到期；沿用 Runtime 的相同入口。
        return self.call(**kwargs, invoke=forbidden)

    def call(
        self,
        *,
        step: str,
        attempt: int,
        provider: str,
        signature: str,
        effect: EffectClass,
        request: bytes,
        invoke: Callable[[], bytes],
        max_bytes: int,
    ) -> CallOutcome:
        try:
            outcome = self.adapter.call(
                step=step,
                attempt=attempt,
                provider=provider,
                signature=signature,
                effect=effect,
                request=request,
                invoke=invoke,
                max_bytes=max_bytes,
            )
            # 写文件响应的观察依赖文件已存在；只在验证录制后重建内存状态。
            if outcome.error is None and provider == "tool:artifact_write/v1":
                write = parse_contract(request, WriteArguments)
                self.workspace.write(write.path, write.text, write.media_type)
            return outcome
        except AgentGitError as error:
            # 录制缺失不能被 Runtime 当成普通工具失败而重规划。
            raise _ReplayAbort(error) from None


class _Journal:
    def __init__(
        self,
        bundle: "RecordedBundle",
        workspace: MemoryWorkspace,
        read: ObjectReader,
        on_checkpoint: Callable[[RebuiltCheckpoint], None] | None,
    ):
        self.bundle, self.workspace, self.read = bundle, workspace, read
        self.on_checkpoint = on_checkpoint
        self.position = 0
        self.rebuilt: list[RebuiltCheckpoint] = []
        self.at_boundary: Callable[[], bool] = lambda: True

    @property
    def head(self) -> str:
        return self.bundle.checkpoints[self.position][0]

    def next_event(self) -> tuple[str, dict[str, Any]]:
        if self.position + 1 >= len(self.bundle.checkpoints):
            raise _ReachedUntil
        commit_id = self.bundle.checkpoints[self.position + 1][0]
        manifest = parse_contract(self.read(commit_id, ObjectType.COMMIT), CommitManifest)
        events = parse_contract(
            self.read(manifest.event_manifest_ref, ObjectType.EVENT_MANIFEST), EventManifest
        )
        if not events.events:
            raise _ReplayAbort(AgentGitError(ErrorCode.OBJECT_CORRUPT, "检查点缺少状态转换事件"))
        event = events.events[-1]
        details = parse_contract(self.read(event.payload_ref, ObjectType.EVENT), EventPayload).data
        return event.type, details

    def save(self, state: AgentState, event_type: str, details: dict[str, Any]) -> None:
        expected_type, _ = self.next_event()
        source_commit, expected = self.bundle.checkpoints[self.position + 1]
        snapshot = self.workspace.snapshot(state)
        state_ref = object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
        expected_ref = object_digest(ObjectType.STATE, encode(expected.model_dump(mode="json")))
        manifest = parse_contract(self.read(source_commit, ObjectType.COMMIT), CommitManifest)
        if (
            event_type != expected_type
            or state_ref != expected_ref
            or state_ref != manifest.state_ref
        ):
            raise _ReplayAbort(
                AgentGitError(ErrorCode.REPLAY_MISMATCH, "检查点重算状态或产物摘要与历史不符")
            )
        rebuilt = RebuiltCheckpoint(source_commit, state_ref, snapshot, event_type, details)
        if self.on_checkpoint is not None:
            try:
                self.on_checkpoint(rebuilt)
            except AgentGitError as error:
                raise _ReplayAbort(error) from None
        self.rebuilt.append(rebuilt)
        self.position += 1
        if self.position + 1 == len(self.bundle.checkpoints):
            raise _ReachedUntil

    def control(self) -> str | None:
        event_type, details = self.next_event()
        error = details.get("error")
        if not self.at_boundary():
            return None
        if (
            event_type == "run.stopped"
            and isinstance(error, dict)
            and error.get("code") == "budget_exhausted"
            and error.get("message") == "持久化墙钟期限已到"
        ):
            return "budget_exhausted"
        return {
            "run.paused": "pause",
            "run.resumed": "resume",
            "run.cancelled": "cancel",
        }.get(event_type)


class RecordedReplayer:
    def __init__(self, read: ObjectReader):
        def guarded_read(reference: str, kind: ObjectType) -> bytes:
            try:
                return read(reference, kind)
            except AgentGitError as error:
                if error.code == ErrorCode.NOT_FOUND:
                    error = AgentGitError(ErrorCode.RECORDING_MISSING, "Recorded 缺少重建所需对象")
                raise _ReplayAbort(error) from None

        self.read = guarded_read

    def validate_environment(
        self,
        bundle: "RecordedBundle",
        config: AgentConfig,
        corpus: Corpus,
        *,
        budget_baseline: BudgetUsage | None = None,
    ) -> None:
        if not bundle.checkpoints:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 没有起点检查点")
        workspace = MemoryWorkspace(
            max_file_bytes=config.limits.max_artifact_bytes, max_files=config.limits.max_files
        )
        try:
            self._compatible(
                bundle, config, corpus, ToolRegistry(corpus, workspace, config), budget_baseline
            )
        except _ReplayAbort as failure:
            raise failure.error from None

    def _compatible(
        self,
        bundle: "RecordedBundle",
        config: AgentConfig,
        corpus: Corpus,
        tools: ToolRegistry,
        budget_baseline: BudgetUsage | None = None,
    ) -> str:
        environment = bundle.environment
        lock = Path(__file__).resolve().parents[3] / "uv.lock"
        if not lock.is_file():
            raise AgentGitError(ErrorCode.CONFIGURATION, "Recorded 兼容性检查缺少依赖锁文件")
        signature = object_digest(
            ObjectType.CALL, encode({"schema_version": 1, "model": config.model})
        )
        if (
            environment.runtime_version != __version__
            or environment.python_version != platform.python_version()
            or environment.code_revision != source_revision()
            or environment.dependency_lock_digest != hashlib.sha256(lock.read_bytes()).hexdigest()
            or environment.model_signature != config.model + ":" + signature
            or environment.prompt_digest != hashlib.sha256(config.prompt.encode()).hexdigest()
            or environment.tool_signatures
            != tuple(tools.signature(name) for name in sorted(config.tools))
            or environment.evaluation_time != corpus.evaluation_time
            or environment.random_seed != 0
        ):
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "Recorded 环境与当前纯 Runtime 不兼容"
            )
        state = parse_contract(encode(bundle.checkpoints[0][1].data), AgentState)
        controlled_input = {
            "schema_version": 1,
            "goal": state.goal.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
            "corpus_digest": corpus.digest(),
        }
        if budget_baseline is not None:
            controlled_input["budget_baseline"] = budget_baseline.model_dump(mode="json")
        frozen_input = encode(controlled_input)
        if self.read(environment.input_ref, ObjectType.BLOB) != frozen_input:
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "Recorded 配置、目标或语料与冻结输入不符"
            )
        return signature

    def run(
        self,
        bundle: "RecordedBundle",
        config: AgentConfig,
        corpus: Corpus,
        *,
        on_checkpoint: Callable[[RebuiltCheckpoint], None] | None = None,
        budget_baseline: BudgetUsage | None = None,
    ) -> ReplayResult:
        try:
            return self._run(bundle, config, corpus, on_checkpoint, budget_baseline)
        except _ReplayAbort as failure:
            raise failure.error from None
        except AgentGitError as error:
            if error.code == ErrorCode.NOT_FOUND:
                raise AgentGitError(
                    ErrorCode.RECORDING_MISSING, "Recorded 缺少重建所需对象"
                ) from None
            raise

    def _run(
        self,
        bundle: "RecordedBundle",
        config: AgentConfig,
        corpus: Corpus,
        on_checkpoint: Callable[[RebuiltCheckpoint], None] | None,
        budget_baseline: BudgetUsage | None,
        depth: int = 0,
    ) -> ReplayResult:
        if (
            not bundle.checkpoints
            or tuple(item[0] for item in bundle.checkpoints) != bundle.selection.commits
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 检查点与选定来源范围不符")
        if depth >= 32:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Recorded 重算证明链超过 32 层")
        if bundle.projection is not None:
            return self._project(bundle, config, corpus, on_checkpoint, budget_baseline, depth)
        workspace = MemoryWorkspace(
            max_file_bytes=config.limits.max_artifact_bytes, max_files=config.limits.max_files
        )
        tools = ToolRegistry(corpus, workspace, config)
        signature = self._compatible(bundle, config, corpus, tools, budget_baseline)
        initial = bundle.checkpoints[0][1]
        initial_manifest = parse_contract(
            self.read(bundle.checkpoints[0][0], ObjectType.COMMIT), CommitManifest
        )
        if initial_manifest.state_ref != object_digest(
            ObjectType.STATE, encode(initial.model_dump(mode="json"))
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 起点与来源状态摘要不符")
        state = parse_contract(encode(initial.data), AgentState)
        for artifact in initial.artifacts:
            workspace.write_bytes(
                artifact.path, self.read(artifact.object_ref, ObjectType.BLOB), artifact.media_type
            )
        journal = _Journal(bundle, workspace, self.read, on_checkpoint)
        adapter = RecordedAdapter(bundle.recordings, lambda: journal.head)
        gateway = _Gateway(adapter, workspace)
        journal.at_boundary = lambda: (
            adapter.position == len(adapter.recordings)
            or adapter.recordings[adapter.position].intent.checkpoint != journal.head
        )

        clock_started = False

        def logical_clock() -> int:
            nonlocal clock_started
            if not clock_started:
                clock_started = True
                return 0
            event_type, details = journal.next_event()
            error = details.get("error")
            # 相同代码的超时事件是受控输入；实际回放耗时不参与逻辑预算。
            if (
                journal.at_boundary()
                and event_type == "run.stopped"
                and isinstance(error, dict)
                and error.get("code") == "budget_exhausted"
                and error.get("message") == "Run 墙钟预算已耗尽"
            ):
                return config.limits.max_wall_ms
            return 0

        def context() -> ExecutionContext:
            policy = config.execution_policy
            if policy is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "Recorded 没有执行策略")
            files = tuple(
                ExecutionFile(
                    path=path,
                    object_ref=object_digest(ObjectType.BLOB, payload),
                    media_type=workspace.media_types[path],
                    byte_length=len(payload),
                )
                for path, payload in workspace.contents().items()
            )
            manifest = ExecutionManifest(files=files)
            return ExecutionContext(
                input_manifest_ref=object_digest(
                    ObjectType.ARTIFACT_MANIFEST, encode(manifest.model_dump(mode="json"))
                ),
                execution_policy_ref=policy.digest(),
                files=files,
            )

        def execution_files(
            result: ExecutionResult, trace: CallTrace
        ) -> tuple[dict[str, bytes], dict[str, str]]:
            if (
                not adapter.position
                or adapter.recordings[adapter.position - 1].saved.response_ref != trace.response_ref
            ):
                raise _ReplayAbort(
                    AgentGitError(ErrorCode.REPLAY_MISMATCH, "执行观察与当前录制不符")
                )
            if result.output_manifest_ref is None:
                raise _ReplayAbort(
                    AgentGitError(ErrorCode.RECORDING_MISSING, "执行录制缺少输出清单")
                )
            outputs = parse_contract(
                self.read(result.output_manifest_ref, ObjectType.ARTIFACT_MANIFEST),
                ExecutionManifest,
            )
            return (
                {item.path: self.read(item.object_ref, ObjectType.BLOB) for item in outputs.files},
                {item.path: item.media_type for item in outputs.files},
            )

        tools.input_context, tools.execution_files = context, execution_files
        runtime = ResearchRuntime(
            state,
            config,
            _RecordedModel(signature),
            tools,
            gateway,
            journal,
            clock_ms=logical_clock,
            control=journal.control,
            budget_baseline=budget_baseline,
        )
        if len(bundle.checkpoints) > 1:
            with suppress(_ReachedUntil):
                while journal.position + 1 < len(bundle.checkpoints):
                    event_type, details = journal.next_event()
                    if event_type == "sandbox.execution_discarded":
                        target = bundle.checkpoints[journal.position + 1][0]
                        candidates = [
                            item for item in bundle.discards if item.source_commit == target
                        ]
                        policy, decision = config.execution_policy, runtime.state.pending_tool
                        if len(candidates) != 1 or policy is None or decision is None:
                            raise AgentGitError(
                                ErrorCode.RECORDING_MISSING, "缺少精确匹配的丢弃录制"
                            )
                        runtime.state = rebuild_discard(
                            candidates[0],
                            runtime.state,
                            context(),
                            signature=tools.signature(decision.tool),
                            max_response_bytes=config.limits.max_response_bytes,
                            max_attempts=policy.max_attempts,
                        )
                        journal.save(runtime.state, event_type, details)
                        continue
                    before = journal.position
                    runtime.run()
                    if journal.position == before or runtime.state.status != RunStatus.PAUSED:
                        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Runtime 未抵达指定终点")
        adapter.assert_consumed()
        if journal.position + 1 != len(bundle.checkpoints):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Runtime 未抵达指定终点")
        final = journal.rebuilt[-1].snapshot if journal.rebuilt else initial
        return ReplayResult(
            bundle.checkpoints[0][0],
            tuple(journal.rebuilt),
            parse_contract(encode(final.data), AgentState),
            adapter.position,
        )

    def _project(
        self,
        bundle: "RecordedBundle",
        config: AgentConfig,
        corpus: Corpus,
        on_checkpoint: Callable[[RebuiltCheckpoint], None] | None,
        budget_baseline: BudgetUsage | None,
        depth: int,
    ) -> ReplayResult:
        projection = bundle.projection
        assert projection is not None
        origin = projection.origin
        self.validate_environment(bundle, config, corpus, budget_baseline=budget_baseline)
        compressed = tuple(
            commit
            for index, commit in enumerate(projection.commits)
            if index == 0 or commit != projection.commits[index - 1]
        )
        if (
            origin.environment != bundle.environment
            or len(projection.commits) != len(bundle.checkpoints)
            or len(projection.events) != len(bundle.checkpoints)
            or compressed != origin.selection.commits
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "Recorded 证明投影没有完整覆盖原路径")
        cursor = 0
        rebuilt: list[RebuiltCheckpoint] = []

        def verify(snapshot: StateSnapshot, event_type: str, details: dict[str, Any]) -> None:
            nonlocal cursor
            commit, expected = bundle.checkpoints[cursor]
            reference = object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
            expected_ref = object_digest(ObjectType.STATE, encode(expected.model_dump(mode="json")))
            manifest = parse_contract(self.read(commit, ObjectType.COMMIT), CommitManifest)
            logical = projection.events[cursor]
            if (
                reference != expected_ref
                or reference != manifest.state_ref
                or (
                    cursor > 0
                    and (
                        logical is None
                        or logical[0] != event_type
                        or encode(logical[1]) != encode(details)
                    )
                )
            ):
                raise AgentGitError(
                    ErrorCode.REPLAY_MISMATCH, "原 Runtime 重算与复合检查点证明不符"
                )
            if cursor:
                item = RebuiltCheckpoint(commit, reference, snapshot, event_type, details)
                if on_checkpoint is not None:
                    on_checkpoint(item)
                rebuilt.append(item)
            cursor += 1

        initial = origin.checkpoints[0][1]
        verify(initial, "replay.restored", {})
        while cursor < len(bundle.checkpoints) and projection.commits[cursor] == compressed[0]:
            verify(initial, "replay.restored", {})

        def checkpoint(item: RebuiltCheckpoint) -> None:
            if (
                cursor >= len(bundle.checkpoints)
                or projection.commits[cursor] != item.source_commit
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "原重算检查点未匹配复合来源路径")
            verify(item.snapshot, item.event_type, item.details)

        result = self._run(origin, config, corpus, checkpoint, budget_baseline, depth + 1)
        if cursor != len(bundle.checkpoints):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "复合 Recorded 没有重算全部检查点")
        return ReplayResult(
            bundle.checkpoints[0][0], tuple(rebuilt), result.state, result.consumed_calls
        )
