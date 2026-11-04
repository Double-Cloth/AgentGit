"""固定成功范围的纯逻辑重算；不读取CAS或构造执行客户端。"""

from collections.abc import Callable
from dataclasses import dataclass

from agentgit.adapters.a2a import A2AAdapter
from agentgit.core.a2a_runs import A2AObservation, A2AProgram, A2ARecordedSpec, A2ARunState
from agentgit.core.a2a_wire import A2ACall, A2AResult
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import BudgetUsage
from agentgit.core.snapshots import ArtifactEntry, StateSnapshot
from agentgit.replay.recorded import RecordedAdapter, Recording


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, message)


def _checkpoint(reference: str) -> Callable[[], str]:
    return lambda: reference


@dataclass(frozen=True)
class A2ASourceFrame:
    commit: str
    state_ref: str
    event: str
    snapshot: StateSnapshot


@dataclass(frozen=True)
class A2ASource:
    spec: A2ARecordedSpec
    program: A2AProgram
    program_ref: str
    frames: tuple[A2ASourceFrame, ...]
    recordings: tuple[Recording, ...]
    actor: str


@dataclass(frozen=True)
class A2ARecomputedFrame:
    source_commit: str
    state: A2ARunState
    snapshot: StateSnapshot
    artifacts: tuple[tuple[str, bytes], ...]


class A2ARecordedEngine:
    def __init__(self, source: A2ASource):
        self.source = source

    def replay(self) -> tuple[A2ARecomputedFrame, ...]:
        source, output = self.source, []
        program = parse_contract(encode(source.program.model_dump(mode="json")), A2AProgram)
        require(
            1 <= len(source.recordings) <= 64
            and len(source.frames) == 1 + 2 * len(source.recordings)
            and source.frames[0].commit == source.spec.from_commit
            and source.frames[-1].commit == source.spec.to_commit
            and source.program_ref
            == object_digest(ObjectType.BLOB, encode(program.model_dump(mode="json"))),
            "A2A Recorded 缺少完整初始化到成功终点的范围",
        )
        state = A2ARunState(program_ref=source.program_ref)
        result: A2AResult | None = None
        position = 0
        adapter = A2AAdapter(program.binding)
        for index, frame in enumerate(source.frames):
            expected_event = (
                "a2a.initialized"
                if index == 0
                else "a2a.call_prepared"
                if index % 2
                else "a2a.observed"
            )
            require(frame.event == expected_event, "A2A Recorded 不支持控制或其他检查点")
            if index % 2:
                require(
                    state.status == RunStatus.RUNNING
                    and state.pending is None
                    and state.usage.tool_calls < program.limits.max_tool_calls,
                    "A2A Recorded 在非运行状态或预算耗尽后准备调用",
                )
                if result is None or state.remote_state == "TASK_STATE_INPUT_REQUIRED":
                    require(
                        state.next_message < len(program.messages), "A2A Recorded 缺少预声明消息"
                    )
                    body = A2ACall(
                        operation="send", body_json=program.messages[state.next_message]
                    ).body()
                    reference = result.reference() if result else None
                    if reference is not None:
                        body["message"].update(
                            taskId=reference.task_id, contextId=reference.context_id
                        )
                    call = A2ACall(
                        operation="send", body_json=encode(body).decode(), task=reference
                    )
                else:
                    require(state.polls < program.max_polls, "A2A Recorded 轮询超过原预算")
                    reference = result.reference()
                    call = A2ACall(
                        operation="get",
                        body_json=encode({"id": reference.task_id}).decode(),
                        task=reference,
                    )
                state = state.model_copy(
                    update=dict(
                        pending=call,
                        defer_pending=False,
                        usage=BudgetUsage(
                            steps=state.usage.steps + 1, tool_calls=state.usage.tool_calls + 1
                        ),
                        polls=state.polls + (call.operation == "get"),
                    )
                )
            elif index:
                require(state.pending is not None, "A2A Recorded 缺少已预留调用")
                assert state.pending is not None
                record = source.recordings[position]
                require(
                    record.saved.complete
                    and record.saved.error is None
                    and not record.reused
                    and record.intent.model_budget is None,
                    "A2A Recorded 仅支持原始完整成功调用",
                )
                playback = RecordedAdapter((record,), _checkpoint(source.frames[index - 1].commit))
                outcome = adapter.call(
                    playback, step=state.pending_step, attempt=1, call=state.pending
                )
                playback.assert_consumed()
                result = parse_contract(outcome.payload, A2AResult)
                remote = result.value()["status"]["state"]
                next_message = state.next_message + (state.pending.operation == "send")
                require(
                    remote in {"TASK_STATE_COMPLETED", "TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"}
                    or (
                        remote == "TASK_STATE_INPUT_REQUIRED"
                        and next_message < len(program.messages)
                    ),
                    "A2A Recorded 成功范围不支持失败、未知或人工等待",
                )
                state = state.model_copy(
                    update=dict(
                        pending=None,
                        observations=(
                            *state.observations,
                            A2AObservation(call=state.pending, trace=outcome.trace),
                        ),
                        last_response_ref=outcome.trace.response_ref,
                        remote_state=remote,
                        next_message=next_message,
                        status=RunStatus.COMPLETED
                        if remote == "TASK_STATE_COMPLETED"
                        else RunStatus.RUNNING,
                        defer_pending=remote != "TASK_STATE_COMPLETED",
                    )
                )
                position += 1
            state = parse_contract(encode(state.model_dump(mode="json")), A2ARunState)
            artifacts = []
            if result is not None:
                artifacts.append(("a2a/task.json", encode(result.model_dump(mode="json"))))
                artifacts.extend(
                    (f"a2a/artifact-{i + 1}.json", encode(value))
                    for i, value in enumerate(result.value().get("artifacts", []))
                )
            require(
                len(artifacts) <= program.limits.max_files
                and sum(len(raw) for _, raw in artifacts) <= program.limits.max_artifact_bytes,
                "A2A Recorded 产物超出原预算",
            )
            snapshot = StateSnapshot(
                data=state.logical_projection(),
                artifacts=tuple(
                    ArtifactEntry(
                        path=name,
                        object_ref=object_digest(ObjectType.BLOB, raw),
                        media_type="application/json",
                    )
                    for name, raw in sorted(artifacts)
                ),
            )
            require(
                snapshot == frame.snapshot
                and object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
                == frame.state_ref,
                "A2A Recorded 重算状态或产物摘要不符",
            )
            output.append(A2ARecomputedFrame(frame.commit, state, snapshot, tuple(artifacts)))
        require(
            position == len(source.recordings) and state.status == RunStatus.COMPLETED,
            "A2A Recorded 未恰好消费完整成功轨迹",
        )
        return tuple(output)
