"""只读核验完整成功 Live 协作的来源闭包，不创建 Run 或执行客户端。"""

import hashlib
import platform
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit import __version__
from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import local_tool_signature
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.collaboration import (
    CausalEventAddress,
    CollaborationBindings,
    CollaborationMessage,
    CollaborationProgram,
    CollaborationRole,
    SharedArtifactHead,
    advance_shared_artifact,
    authorize_message,
)
from agentgit.core.collaboration_execution import CollaborationRoleState, RoleModelRequest
from agentgit.core.collaboration_recordings import (
    CollaborationRoleSource,
    CollaborationSourceFrame,
    CollaborationSourceIndex,
)
from agentgit.core.collaboration_runs import (
    CollaborationFrozenInput,
    CollaborationRolePayload,
    RolePrivateInput,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenCorpus
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import BudgetLimits, SavedCallOutcome, evolve
from agentgit.core.snapshots import EventManifest, EventPayload, LogicalEvent, StateSnapshot
from agentgit.replay.recorded import Recording
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.storage.roles import path_reference_role


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, message)


@dataclass(frozen=True)
class CollaborationSource:
    index: CollaborationSourceIndex
    # 每一项只含所属角色的录制，不能将四个来源合并供任意角色消费。
    recordings: tuple[tuple[Recording, ...], ...]
    states: tuple[tuple[CollaborationRoleState, ...], ...]


class CollaborationRecordingReader:
    def __init__(self, history: HistoryService):
        self.history = history

    def inspect(self, repository: UUID, group: UUID, *, actor: str) -> CollaborationSource:
        # 一致性快照及数据库只读限制覆盖全部核验；身份检查先于任何私有 CAS 读取。
        with (
            self.history.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection,
            connection.begin(),
        ):
            connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            return _SourceReader(self.history, connection, repository, group, actor).load()


class _SourceReader:
    def __init__(self, history: HistoryService, c: Connection, repo: UUID, group: UUID, actor: str):
        self.history, self.c, self.repo, self.group, self.actor = history, c, repo, group, actor
        self.objects: dict[str, bytes] = {}
        self.bytes = 0
        self.events: dict[tuple[str, int], dict[str, Any]] = {}
        self.frames: dict[tuple[str, str], tuple[CollaborationRoleState, EventManifest]] = {}

    def rows(self, name: str, *conditions: Any, limit: int = 50001) -> list[dict[str, Any]]:
        table = TABLES[name]
        rows = list(
            self.c.execute(
                sa.select(table).where(table.c.repository_id == self.repo, *conditions).limit(limit)
            ).mappings()
        )
        _require(len(rows) < limit, "协作来源索引超过有界上限")
        return [dict(row) for row in rows]

    def owned(self, ref: str, kind: ObjectType, owner: str, identity: str, role: str) -> bytes:
        refs, objects = TABLES["object_refs"], TABLES["objects"]
        row = (
            self.c.execute(
                sa.select(objects.c.byte_length, objects.c.object_type)
                .join(refs, refs.c.object_id == objects.c.id)
                .where(
                    refs.c.repository_id == self.repo,
                    refs.c.object_id == ref,
                    refs.c.owner_type == owner,
                    refs.c.owner_id == identity,
                    refs.c.role == role,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "协作录制缺少实际来源引用")
        _require(row["object_type"] == kind.value, "协作来源对象类型不符")
        if ref not in self.objects:
            if self.bytes + row["byte_length"] > 64 * 1024 * 1024:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "协作来源闭包超过 64 MiB")
            raw = self.history.objects.get(ref, kind)
            _require(len(raw) == row["byte_length"], "协作来源对象长度索引不符")
            self.objects[ref] = raw
            self.bytes += len(raw)
        return self.objects[ref]

    def load(self) -> CollaborationSource:
        groups = self.rows("collaborations", TABLES["collaborations"].c.id == self.group)
        if not groups or groups[0]["actor"] != self.actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "协作录制只供原操作者在所属项目读取")
        group = groups[0]
        _require(
            group["status"] == "completed"
            and group["failure_run_id"] is None
            and group["failure_event_seq"] is None,
            "首批仅支持完整成功协作来源",
        )

        def own(ref: str, kind: ObjectType, role: str) -> bytes:
            return self.owned(ref, kind, "collaboration", str(self.group), role)

        self.program = parse_contract(
            own(group["program_ref"], ObjectType.BLOB, "program"), CollaborationProgram
        )
        self.bindings = parse_contract(
            own(group["bindings_ref"], ObjectType.BLOB, "bindings"), CollaborationBindings
        )
        environment = parse_contract(
            own(group["environment_ref"], ObjectType.ENVIRONMENT, "environment"),
            EnvironmentManifest,
        )
        _require(
            (self.bindings.repository_id, self.bindings.collaboration_id)
            == (str(self.repo), str(self.group)),
            "协作来源绑定跨项目或组",
        )
        self.frozen = parse_contract(
            own(environment.input_ref, ObjectType.BLOB, "input"), CollaborationFrozenInput
        )
        corpus = parse_contract(
            own(self.frozen.corpus_ref, ObjectType.BLOB, "corpus"), FrozenCorpus
        )
        self.corpus = Corpus(corpus.sources, evaluation_time=corpus.evaluation_time)
        _require(
            environment.code_revision == source_revision()
            and environment.dependency_lock_digest == dependency_lock_digest(),
            "协作 Recorded 要求相同源码和依赖锁",
        )
        _require(
            (
                environment.runtime_version,
                environment.python_version,
                environment.model_signature,
                environment.tool_signatures,
                environment.prompt_digest,
                environment.evaluation_time,
                environment.random_seed,
            )
            == (
                __version__,
                platform.python_version(),
                "offline-four-role-research/v1",
                (),
                hashlib.sha256(encode(self.program.goal.model_dump(mode="json"))).hexdigest(),
                corpus.evaluation_time,
                0,
            ),
            "协作来源环境与固定程序不符",
        )
        _require(
            (self.frozen.program_ref, self.frozen.bindings_ref, self.frozen.corpus_digest)
            == (group["program_ref"], group["bindings_ref"], self.corpus.digest()),
            "协作来源冻结输入不符",
        )
        tasks = self.rows(
            "collaboration_tasks",
            TABLES["collaboration_tasks"].c.collaboration_id == self.group,
            limit=5,
        )
        _require(
            len(tasks) == 4 and {t["role"] for t in tasks} == {r.value for r in CollaborationRole},
            "协作录制必须包含四个实际角色任务",
        )
        sources, recordings, states = [], [], []
        for binding in self.bindings.agents:
            task = next(t for t in tasks if t["role"] == binding.role.value)
            source, calls, checkpoints = self.role(task, binding, environment, group)
            sources.append(source)
            recordings.append(calls)
            states.append(checkpoints)
        _require(len(self.events) <= 50000, "协作来源事件超过有界上限")
        for (run, seq), event in self.events.items():
            cause = event["caused_by_seq"]
            if cause is not None:
                origin = str(event["caused_by_run_id"] or run)
                _require(
                    (origin, cause) in self.events and (origin != run or cause < seq),
                    "协作来源事件缺少所属四角色内的先行因果",
                )
        messages, heads = self.exchange(states)
        index = CollaborationSourceIndex(
            repository_id=str(self.repo),
            collaboration_id=str(self.group),
            program_ref=group["program_ref"],
            bindings_ref=group["bindings_ref"],
            environment_ref=group["environment_ref"],
            input_ref=environment.input_ref,
            corpus_ref=self.frozen.corpus_ref,
            code_revision=environment.code_revision,
            dependency_lock_digest=environment.dependency_lock_digest,
            roles=tuple(sources),
            message_ids=messages,
            shared_head_refs=heads,
            object_refs=tuple(sorted(self.objects)),
            total_bytes=self.bytes,
        )
        return CollaborationSource(index, tuple(recordings), tuple(states))

    def role(
        self,
        task: dict[str, Any],
        binding: Any,
        environment: EnvironmentManifest,
        group: dict[str, Any],
    ) -> tuple[CollaborationRoleSource, tuple[Recording, ...], tuple[CollaborationRoleState, ...]]:
        run_id, task_id, job_id = str(task["run_id"]), str(task["id"]), str(task["job_id"])
        _require(
            (run_id, task_id) == (binding.agent_run_id, binding.task_id),
            "协作任务与实际 Run 绑定不符",
        )
        run_rows = self.rows("runs", TABLES["runs"].c.id == task["run_id"], limit=2)
        job_rows = self.rows("jobs", TABLES["jobs"].c.id == task["job_id"], limit=2)
        _require(len(run_rows) == len(job_rows) == 1, "协作角色缺少实际 Run/Job")
        run, job = run_rows[0], job_rows[0]
        _require(
            run["mode"] == "live"
            and run["status"] == job["status"] == "completed"
            and run["base_commit_id"] is None
            and run["origin_run_id"] is None
            and run["task_id"] is None
            and run["agent_config_id"] is None
            and run["failure_code"] is None
            and job["run_id"] == task["run_id"]
            and job["origin_run_id"] is None
            and job["kind"] == "collaboration_role"
            and job["checkpoint_commit_id"] == run["checkpoint_commit_id"]
            and run["last_event_seq"] == run["checkpoint_event_seq"],
            "协作角色来源不是完整成功的独立 Live Run",
        )
        policy = self.program.policy(binding.role)
        _require(
            self.owned(task["policy_ref"], ObjectType.BLOB, "collaboration_task", task_id, "policy")
            == encode(policy.model_dump(mode="json")),
            "角色来源策略不符",
        )
        budget = parse_contract(
            self.owned(
                task["budget_ref"], ObjectType.BLOB, "collaboration_task", task_id, "budget"
            ),
            BudgetLimits,
        )
        _require(budget == policy.limits, "角色来源预算不符")
        private = parse_contract(
            self.owned(
                task["private_input_ref"],
                ObjectType.BLOB,
                "collaboration_task",
                task_id,
                "private_input",
            ),
            RolePrivateInput,
        )
        _require(private.role == binding.role, "角色私有输入归属不符")
        payload = parse_contract(
            self.owned(job["payload_ref"], ObjectType.BLOB, "job", job_id, "payload"),
            CollaborationRolePayload,
        )
        _require(
            payload
            == CollaborationRolePayload(
                collaboration_id=str(self.group),
                role=binding.role,
                program_ref=group["program_ref"],
                bindings_ref=group["bindings_ref"],
                environment_ref=group["environment_ref"],
                policy_ref=task["policy_ref"],
                private_input_ref=task["private_input_ref"],
                code_revision=environment.code_revision,
                dependency_lock_digest=environment.dependency_lock_digest,
                max_wall_ms=budget.max_wall_ms,
            ),
            "角色 Job 载荷与冻结来源不符",
        )
        paths = sorted(
            self.rows("run_commits", TABLES["run_commits"].c.run_id == task["run_id"], limit=10001),
            key=lambda r: r["run_order"],
        )
        snapshots = self.rows(
            "snapshots", TABLES["snapshots"].c.run_id == task["run_id"], limit=10001
        )
        rows = sorted(
            self.rows("events", TABLES["events"].c.run_id == task["run_id"]), key=lambda r: r["seq"]
        )
        _require(
            bool(paths)
            and len(paths) == len(snapshots)
            and paths[-1]["commit_id"] == run["checkpoint_commit_id"]
            and [p["run_order"] for p in paths] == list(range(1, len(paths) + 1)),
            "角色录制必须有完整无重复根到终点路径",
        )
        _require(
            [e["seq"] for e in rows] == list(range(1, run["last_event_seq"] + 1)),
            "角色来源事件必须从首事件连续覆盖",
        )
        self.events.update({(run_id, e["seq"]): e for e in rows})
        descriptors, checkpoints = [], []
        previous, seq = None, 0
        for path in paths:
            commit = path["commit_id"]
            manifest = parse_contract(
                self.owned(commit, ObjectType.COMMIT, "commit", commit, "manifest"), CommitManifest
            )
            _require(
                manifest.repository_id == str(self.repo)
                and manifest.parents == (() if previous is None else (previous,))
                and manifest.event_range.first_seq == seq + 1,
                "角色来源父链或事件边界不符",
            )
            restored = self.history.restore(self.repo, commit)
            snapshot = parse_contract(
                self.owned(manifest.state_ref, ObjectType.STATE, "commit", commit, "state"),
                StateSnapshot,
            )
            _require(
                restored == snapshot
                and any(
                    s["commit_id"] == commit
                    and s["state_ref"] == manifest.state_ref
                    and s["event_seq"] == manifest.event_range.last_seq
                    for s in snapshots
                ),
                "角色快照索引不符",
            )
            frame_env = parse_contract(
                self.owned(
                    manifest.environment_ref,
                    ObjectType.ENVIRONMENT,
                    "commit",
                    commit,
                    "environment",
                ),
                EnvironmentManifest,
            )
            names = tuple(
                p
                for p in policy.permissions
                if p in {"corpus_search", "source_read", "artifact_write"}
            )
            expected_env = evolve(
                environment,
                input_ref=job["payload_ref"],
                model_signature=MockCollaborationModel.signature,
                tool_signatures=(
                    MockCollaborationModel.signature,
                    *(local_tool_signature(n) for n in names),
                ),
                prompt_digest=hashlib.sha256(
                    encode(
                        dict(
                            role=binding.role.value,
                            private_input_ref=task["private_input_ref"],
                            goal=self.program.goal.model_dump(mode="json"),
                        )
                    )
                ).hexdigest(),
            )
            _require(frame_env == expected_env, "角色来源执行环境不符")
            events = parse_contract(
                self.owned(
                    manifest.event_manifest_ref,
                    ObjectType.EVENT_MANIFEST,
                    "commit",
                    commit,
                    "events",
                ),
                EventManifest,
            )
            selected = rows[seq : manifest.event_range.last_seq]
            _require(
                tuple(
                    LogicalEvent(**self.history._logical_event(cast(RowMapping, e), task["run_id"]))
                    for e in selected
                )
                == events.events,
                "角色来源事件索引与 Commit 清单不符",
            )
            for event in selected:
                self.owned(
                    event["payload_ref"],
                    ObjectType.EVENT,
                    "event",
                    f"{run_id}:{event['seq']}",
                    "payload",
                )
            state = parse_contract(encode(snapshot.data), CollaborationRoleState)
            _require(
                (state.collaboration_id, state.role, state.program_ref, state.private_input_ref)
                == (str(self.group), binding.role, group["program_ref"], task["private_input_ref"]),
                "角色来源快照身份不符",
            )
            for name, ref in state.exports.items():
                self.owned(ref, ObjectType.BLOB, "collaboration_export", run_id, name)
            for artifact in snapshot.artifacts:
                # restore 已核对产物索引和路径；仍将其字节计入闭包上限。
                self.owned(
                    artifact.object_ref,
                    ObjectType.BLOB,
                    "commit",
                    commit,
                    path_reference_role("artifact", artifact.path),
                )
            self.frames[(run_id, commit)] = state, events
            descriptors.append(
                CollaborationSourceFrame(
                    commit_id=commit,
                    state_ref=manifest.state_ref,
                    environment_ref=manifest.environment_ref,
                    event_manifest_ref=manifest.event_manifest_ref,
                    first_seq=manifest.event_range.first_seq,
                    last_seq=manifest.event_range.last_seq,
                )
            )
            checkpoints.append(state)
            previous, seq = commit, manifest.event_range.last_seq
        _require(
            seq == run["last_event_seq"]
            and checkpoints[-1].status.value == "completed"
            and checkpoints[-1].phase == "done"
            and checkpoints[-1].pending is None,
            "角色来源终点尚有未覆盖事件或未决动作",
        )
        calls = self.calls(task, checkpoints, private)
        return (
            CollaborationRoleSource(
                role=binding.role,
                task_id=task_id,
                run_id=run_id,
                job_id=job_id,
                payload_ref=job["payload_ref"],
                policy_ref=task["policy_ref"],
                budget_ref=task["budget_ref"],
                private_input_ref=task["private_input_ref"],
                frames=tuple(descriptors),
                call_ids=tuple(c.source_call_id for c in calls),
            ),
            calls,
            tuple(checkpoints),
        )

    def calls(
        self, task: dict[str, Any], states: list[CollaborationRoleState], private: RolePrivateInput
    ) -> tuple[Recording, ...]:
        rows = self.rows(
            "external_calls", TABLES["external_calls"].c.run_id == task["run_id"], limit=33
        )
        traces = states[-1].traces
        _require(
            len(rows)
            == len(traces)
            == {"supervisor": 2, "researcher": 8, "verifier": 7, "writer": 3}[task["role"]],
            "角色来源调用集合不完整或超出固定成功流程",
        )
        result = []
        for trace in traces:
            selected = [
                r
                for r in rows
                if (r["logical_step_id"], r["call_index"], r["attempt"])
                == (trace.logical_step_id, trace.call_index, trace.attempt)
            ]
            _require(len(selected) == 1, "角色录制调用 occurrence 缺失或重复")
            row = selected[0]
            _require(
                row["origin_call_id"] is None
                and row["status"] == "succeeded"
                and row["result_ref"] is not None,
                "角色来源调用未完整结算或混入其他 Run",
            )

            def own(ref: str, kind: ObjectType, role: str, identity: str = str(row["id"])) -> bytes:
                return self.owned(ref, kind, "external_call", identity, role)

            intent = parse_contract(own(row["intent_ref"], ObjectType.CALL, "intent"), CallIntent)
            saved = parse_contract(
                own(row["result_ref"], ObjectType.CALL, "result"), SavedCallOutcome
            )
            recording = Recording(
                intent,
                saved,
                own(intent.request_ref, ObjectType.BLOB, "request"),
                own(saved.response_ref, ObjectType.BLOB, "response"),
                str(row["id"]),
            )
            _require(
                recording.outcome().trace == trace
                and saved.error is None
                and saved.complete
                and (
                    intent.occurrence.logical_step_id,
                    intent.occurrence.call_index,
                    intent.occurrence.attempt,
                    intent.occurrence.provider,
                    intent.occurrence.signature_digest,
                    intent.request_ref,
                    intent.effect.value,
                )
                == tuple(
                    row[k]
                    for k in (
                        "logical_step_id",
                        "call_index",
                        "attempt",
                        "provider",
                        "signature_digest",
                        "request_digest",
                        "effect",
                    )
                ),
                "角色录制意图、结果、轨迹与物理调用不符",
            )
            frame = self.frames.get((str(task["run_id"]), intent.checkpoint or ""))
            _require(
                frame is not None and frame[0].pending is not None, "角色调用缺少自身预留检查点"
            )
            assert frame is not None and frame[0].pending is not None
            pending = frame[0].pending
            _require(
                (
                    pending.step,
                    pending.provider,
                    pending.signature,
                    pending.request_ref,
                    pending.effect,
                    pending.max_bytes,
                )
                == (
                    trace.logical_step_id,
                    trace.provider,
                    trace.signature_digest,
                    trace.request_ref,
                    intent.effect,
                    intent.max_response_bytes,
                ),
                "角色录制调用与预留不符",
            )
            if pending.category == "model":
                request = parse_contract(recording.request, RoleModelRequest)
                _require(
                    request.role == private.role
                    and request.private_notes == private.notes
                    and request.goal == self.program.goal
                    and pending.signature == MockCollaborationModel.signature,
                    "角色模型来源混入其他私有上下文或模型契约",
                )
            else:
                _require(
                    pending.name in self.program.policy(private.role).permissions
                    and pending.signature == local_tool_signature(pending.name),
                    "角色录制工具未被自身策略授权",
                )
            result.append(recording)
        for state in states:
            _require(
                state.traces == traces[: len(state.traces)], "角色来源调用轨迹不是自身有序前缀"
            )
        return tuple(result)

    def event(self, run: str, seq: int, commit: str, kind: str) -> dict[str, Any]:
        event = self.events.get((run, seq))
        frame = self.frames.get((run, commit))
        _require(
            event is not None
            and event["type"] == kind
            and frame is not None
            and any(
                e.seq == seq and e.payload_ref == event["payload_ref"] for e in frame[1].events
            ),
            "协作交换来源事件不属于声明的实际版本",
        )
        assert event is not None
        return event

    def parent(self, address: CausalEventAddress, ref: str) -> CollaborationRoleState:
        event = self.event(
            address.agent_run_id, address.seq, address.commit_id, "collaboration.export_ready"
        )
        state = self.frames[(address.agent_run_id, address.commit_id)][0]
        _require(
            event["payload_ref"] == address.payload_ref
            and state.phase == "export_ready"
            and ref in state.exports.values(),
            "协作交换父事件不是实际明确导出",
        )
        return state

    def exchange(
        self, states: list[tuple[CollaborationRoleState, ...]]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        messages = self.rows(
            "collaboration_messages",
            TABLES["collaboration_messages"].c.collaboration_id == self.group,
            limit=33,
        )
        runs = [UUID(a.agent_run_id) for a in self.bindings.agents]
        receipts = self.rows(
            "collaboration_receipts",
            TABLES["collaboration_receipts"].c.receiver_run_id.in_(runs),
            limit=33,
        )
        publications = sorted(
            self.rows(
                "collaboration_publications",
                TABLES["collaboration_publications"].c.collaboration_id == self.group,
                limit=17,
            ),
            key=lambda r: (r["path"], r["version"]),
        )
        _require(
            len(messages) == len(receipts) == 7 and len(publications) == 5,
            "协作来源消息、收据或共享版本不完整",
        )
        sent: dict[str, set[str]] = {str(r): set() for r in runs}
        received: dict[str, set[str]] = {str(r): set() for r in runs}
        for row in messages:
            identity = str(row["id"])
            envelope = parse_contract(
                self.owned(
                    row["envelope_ref"],
                    ObjectType.BLOB,
                    "collaboration_message",
                    identity,
                    "envelope",
                ),
                CollaborationMessage,
            )
            authorize_message(self.program, self.bindings, envelope)
            _require(
                (
                    envelope.message_id,
                    envelope.sender,
                    envelope.receiver,
                    envelope.task_id,
                    envelope.payload_ref,
                )
                == (
                    identity,
                    str(row["sender_run_id"]),
                    str(row["receiver_run_id"]),
                    str(row["receiver_task_id"]),
                    row["payload_ref"],
                ),
                "协作消息索引与实际信封不符",
            )
            self.owned(
                row["payload_ref"], ObjectType.BLOB, "collaboration_message", identity, "payload"
            )
            self.parent(envelope.parent_event_id, envelope.payload_ref)
            event = self.event(
                envelope.sender,
                row["sent_seq"],
                row["sent_commit_id"],
                "collaboration.message_sent",
            )
            _require(
                event["caused_by_seq"] == envelope.parent_event_id.seq
                and str(event["caused_by_run_id"] or row["sender_run_id"]) == envelope.sender,
                "协作发送因果与导出父事件不符",
            )
            selected = [r for r in receipts if r["message_id"] == row["id"]]
            _require(
                len(selected) == 1 and str(selected[0]["receiver_run_id"]) == envelope.receiver,
                "协作消息缺少唯一实际接收收据",
            )
            ack = selected[0]
            event = self.event(
                envelope.receiver,
                ack["received_seq"],
                ack["received_commit_id"],
                "collaboration.message_received",
            )
            _require(
                (str(event["caused_by_run_id"]), event["caused_by_seq"])
                == (envelope.sender, row["sent_seq"]),
                "协作接收因果缺少实际发送事件",
            )
            for run, seq in (
                (envelope.sender, row["sent_seq"]),
                (envelope.receiver, ack["received_seq"]),
            ):
                payload = parse_contract(
                    self.objects[self.events[(run, seq)]["payload_ref"]], EventPayload
                )
                _require(payload.data == dict(message_id=identity), "协作消息事件载荷与信封不符")
            sent[envelope.sender].add(identity)
            received[envelope.receiver].add(identity)
        for checkpoints in states:
            final = checkpoints[-1]
            run = next(a.agent_run_id for a in self.bindings.agents if a.role == final.role)
            _require(
                set(final.sent) == sent[run] and set(final.received) == received[run],
                "角色终点与耐久消息收据不符",
            )
        current: dict[str, SharedArtifactHead] = {}
        refs = []
        for row in publications:
            head = parse_contract(
                self.owned(
                    row["head_ref"],
                    ObjectType.BLOB,
                    "collaboration_share",
                    str(self.group),
                    f"head:{row['path']}:{row['version']}",
                ),
                SharedArtifactHead,
            )
            self.owned(
                row["object_ref"],
                ObjectType.BLOB,
                "collaboration_share",
                str(self.group),
                f"object:{row['path']}:{row['version']}",
            )
            parent = self.parent(head.publisher_event, row["object_ref"])
            _require(
                head
                == advance_shared_artifact(
                    self.program,
                    self.bindings,
                    current.get(row["path"]),
                    path=row["path"],
                    expected_version=row["version"] - 1,
                    object_ref=row["object_ref"],
                    publisher_event=head.publisher_event,
                )
                and head.publisher_event.agent_run_id == str(row["publisher_run_id"])
                and parent.exports.get(row["path"]) == row["object_ref"],
                "协作共享版本或发布归属不符",
            )
            event = self.event(
                str(row["publisher_run_id"]),
                row["event_seq"],
                row["commit_id"],
                "collaboration.shared_published",
            )
            _require(
                event["caused_by_seq"] == head.publisher_event.seq
                and parse_contract(self.objects[event["payload_ref"]], EventPayload).data
                == dict(path=row["path"], version=row["version"]),
                "协作共享发布事件与版本不符",
            )
            current[row["path"]] = head
            refs.append(row["head_ref"])
        _require(
            {p: h.version for p, h in current.items()}
            == {"evidence.json": 2, "verification.json": 1, "report.md": 1, "sources.json": 1},
            "固定成功流程缺少完整共享版本",
        )
        return tuple(sorted(str(m["id"]) for m in messages)), tuple(refs)
