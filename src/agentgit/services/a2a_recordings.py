"""本人完整成功A2A来源的只读闭包；内容及关系索引共同核验。"""

import platform
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit import __version__
from agentgit.core.a2a_runs import A2AJobPayload, A2AProgram, A2ARecordedSpec
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.effects import BoundAction, ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.core.snapshots import EventManifest, EventPayload, LogicalEvent, StateSnapshot
from agentgit.replay.a2a import A2ARecordedEngine, A2ASource, A2ASourceFrame, require
from agentgit.replay.recorded import Recording
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.storage.roles import path_reference_role


class A2ARecordingReader:
    def __init__(self, history: HistoryService):
        self.history = history

    def inspect(self, repository: UUID, spec: A2ARecordedSpec, *, actor: str) -> A2ASource:
        spec = parse_contract(encode(spec.model_dump(mode="json")), A2ARecordedSpec)
        with (
            self.history.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c,
            c.begin(),
        ):
            c.execute(sa.text("SET TRANSACTION READ ONLY"))
            return _Reader(self.history, c, repository, spec, actor).load()


class _Reader:
    def __init__(
        self, history: HistoryService, c: Connection, repo: UUID, spec: A2ARecordedSpec, actor: str
    ):
        self.history, self.c, self.repo, self.spec, self.actor = history, c, repo, spec, actor
        self.run_id = UUID(spec.source_run_id)
        self.cache: dict[str, bytes] = {}
        self.bytes = 0

    def rows(self, name: str, *conditions: Any, limit: int = 1025) -> list[dict[str, Any]]:
        table = TABLES[name]
        rows = [
            dict(row)
            for row in self.c.execute(
                sa.select(table).where(table.c.repository_id == self.repo, *conditions).limit(limit)
            ).mappings()
        ]
        if len(rows) == limit:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "A2A 来源关系索引超过有界上限")
        return rows

    def owned(self, ref: str, kind: ObjectType, owner: str, identity: str, role: str) -> bytes:
        refs, objects = TABLES["object_refs"], TABLES["objects"]
        row = (
            self.c.execute(
                sa.select(objects.c.object_type, objects.c.byte_length)
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
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "A2A 录制缺少同项目来源引用")
        require(row["object_type"] == kind.value, "A2A 录制引用对象类型不符")
        if ref not in self.cache:
            if self.bytes + row["byte_length"] > 64 * 1024 * 1024:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "A2A 来源闭包超过64MiB")
            raw = self.history.objects.get(ref, kind)
            require(len(raw) == row["byte_length"], "A2A 录制字节与关系索引不符")
            self.bytes += len(raw)
            self.cache[ref] = raw
        return self.cache[ref]

    def load(self) -> A2ASource:
        jobs = self.rows("jobs", TABLES["jobs"].c.run_id == self.run_id)
        if len(jobs) != 1 or jobs[0]["kind"] != "a2a_run":
            raise AgentGitError(ErrorCode.NOT_FOUND, "A2A 录制来源不可访问")
        job = jobs[0]
        payload = parse_contract(
            self.owned(job["payload_ref"], ObjectType.BLOB, "job", str(job["id"]), "payload"),
            A2AJobPayload,
        )
        if payload.actor != self.actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 录制来源不属于当前操作者")
        run = self.history._run(self.c, self.repo, self.run_id)
        require(
            run["mode"] == "live"
            and run["status"] == job["status"] == "completed"
            and run["origin_run_id"] is None
            and job["origin_run_id"] is None,
            "A2A Recorded 只接受原始完整成功Live来源",
        )
        if (
            payload.code_revision != source_revision()
            or payload.dependency_lock_digest != dependency_lock_digest()
        ):
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "A2A 录制来源源码或锁与当前不符")
        program = parse_contract(
            self.owned(payload.program_ref, ObjectType.BLOB, "repository", str(self.repo), "input"),
            A2AProgram,
        )
        require(payload.max_wall_ms == program.limits.max_wall_ms, "A2A 来源程序与Job预算不符")
        paths = sorted(
            self.rows("run_commits", TABLES["run_commits"].c.run_id == self.run_id, limit=130),
            key=lambda r: r["run_order"],
        )
        require(
            bool(paths)
            and all(p["run_order"] == i for i, p in enumerate(paths, 1))
            and paths[0]["commit_id"] == self.spec.from_commit
            and paths[-1]["commit_id"] == self.spec.to_commit == run["checkpoint_commit_id"]
            and len({p["commit_id"] for p in paths}) == len(paths),
            "A2A Recorded 必须显式选择自身初始化到成功终点的完整路径",
        )
        events = sorted(
            self.rows("events", TABLES["events"].c.run_id == self.run_id), key=lambda r: r["seq"]
        )
        require(
            len(events) == run["last_event_seq"]
            and all(e["seq"] == i for i, e in enumerate(events, 1)),
            "A2A 来源事件索引缺失",
        )
        snapshots = {
            r["commit_id"]: r
            for r in self.rows("snapshots", TABLES["snapshots"].c.run_id == self.run_id, limit=130)
        }
        ids = [p["commit_id"] for p in paths]
        commits = {
            r["id"]: r for r in self.rows("commits", TABLES["commits"].c.id.in_(ids), limit=130)
        }
        parents = self.rows(
            "commit_parents", TABLES["commit_parents"].c.commit_id.in_(ids), limit=259
        )
        artifacts = self.rows("artifacts", TABLES["artifacts"].c.commit_id.in_(ids), limit=1162)
        require(len(snapshots) == len(paths), "A2A 来源快照索引不完整")
        expected_environment = EnvironmentManifest(
            runtime_version=__version__,
            python_version=platform.python_version(),
            code_revision=payload.code_revision,
            dependency_lock_digest=payload.dependency_lock_digest,
            model_signature="no-model/fixed-a2a/v1",
            prompt_digest=object_digest(ObjectType.BLOB, b"fixed-a2a/v1"),
            input_ref=payload.program_ref,
            evaluation_time="2026-10-31T00:00:00Z",
            random_seed=0,
        )
        frames = []
        previous, sequence = run["base_commit_id"], 0
        for frame_index, path in enumerate(paths):
            commit = path["commit_id"]
            manifest = parse_contract(
                self.owned(commit, ObjectType.COMMIT, "commit", commit, "manifest"), CommitManifest
            )
            indexed = commits.get(commit)
            parent_ids = tuple(
                r["parent_id"]
                for r in sorted(
                    (r for r in parents if r["commit_id"] == commit),
                    key=lambda r: r["parent_order"],
                )
            )
            require(
                manifest.repository_id == str(self.repo)
                and manifest.parents == parent_ids == (() if previous is None else (previous,))
                and indexed is not None
                and all(
                    indexed[k] == getattr(manifest, k)
                    for k in ("state_ref", "environment_ref", "event_manifest_ref")
                )
                and manifest.event_range.first_seq == sequence + 1,
                "A2A 来源Commit、单父路径或事件边界不符",
            )
            require(
                sorted(r["parent_order"] for r in parents if r["commit_id"] == commit)
                == ([] if previous is None else [0]),
                "A2A 来源父边编号不符",
            )
            snapshot = parse_contract(
                self.owned(manifest.state_ref, ObjectType.STATE, "commit", commit, "state"),
                StateSnapshot,
            )
            snap = snapshots.get(commit)
            require(
                snap is not None
                and snap["state_ref"] == manifest.state_ref
                and snap["event_seq"] == manifest.event_range.last_seq,
                "A2A 来源快照索引与内容不符",
            )
            environment = parse_contract(
                self.owned(
                    manifest.environment_ref,
                    ObjectType.ENVIRONMENT,
                    "commit",
                    commit,
                    "environment",
                ),
                EnvironmentManifest,
            )
            require(environment == expected_environment, "A2A 来源执行环境不符")
            event_manifest = parse_contract(
                self.owned(
                    manifest.event_manifest_ref,
                    ObjectType.EVENT_MANIFEST,
                    "commit",
                    commit,
                    "events",
                ),
                EventManifest,
            )
            selected = events[sequence : manifest.event_range.last_seq]
            require(
                len(selected) == manifest.event_range.last_seq - sequence
                and tuple(
                    LogicalEvent(**self.history._logical_event(cast(RowMapping, e), self.run_id))
                    for e in selected
                )
                == event_manifest.events,
                "A2A 来源实际事件与Commit清单不符",
            )
            for event in selected:
                parse_contract(
                    self.owned(
                        event["payload_ref"],
                        ObjectType.EVENT,
                        "event",
                        f"{self.run_id}:{event['seq']}",
                        "payload",
                    ),
                    EventPayload,
                )
            own_events = [e for e in selected if e["type"].startswith("a2a.")]
            require(
                len(own_events) == 1 and bool(selected) and own_events[0] == selected[-1],
                "A2A 来源检查点缺少唯一状态转换事件",
            )
            transition = own_events[0]
            transition_payload = parse_contract(
                self.owned(
                    transition["payload_ref"],
                    ObjectType.EVENT,
                    "event",
                    f"{self.run_id}:{transition['seq']}",
                    "payload",
                ),
                EventPayload,
            )
            require(
                transition["logical_step_id"] == f"a2a-{frame_index // 2:04d}"
                and transition_payload.data
                == {
                    "program_ref": payload.program_ref,
                    "response_ref": snapshot.data.get("last_response_ref"),
                    "error_code": snapshot.data.get("error_code"),
                    "defer_pending": snapshot.data.get("defer_pending"),
                    "remote_cancel_status": snapshot.data.get("remote_cancel_status"),
                },
                "A2A 来源转换事件与状态不符",
            )
            actual_artifacts = sorted(
                (r["relative_path"], r["object_id"], r["media_type"])
                for r in artifacts
                if r["commit_id"] == commit
            )
            require(
                actual_artifacts
                == [(a.path, a.object_ref, a.media_type) for a in snapshot.artifacts],
                "A2A 来源产物关系索引不符",
            )
            for artifact in snapshot.artifacts:
                self.owned(
                    artifact.object_ref,
                    ObjectType.BLOB,
                    "commit",
                    commit,
                    path_reference_role("artifact", artifact.path),
                )
            frames.append(
                A2ASourceFrame(commit, manifest.state_ref, own_events[0]["type"], snapshot)
            )
            previous, sequence = commit, manifest.event_range.last_seq
        require(sequence == len(events), "A2A 来源终点后存在未覆盖事件")
        calls = self.rows(
            "external_calls", TABLES["external_calls"].c.run_id == self.run_id, limit=65
        )
        recordings = []
        approvals = self.rows("approvals", TABLES["approvals"].c.run_id == self.run_id, limit=65)
        policy = ToolPolicy(
            rules=(
                program.binding.rule("send"),
                program.binding.rule("get"),
                program.binding.rule("cancel"),
            )
        )
        for row in sorted(calls, key=lambda r: r["logical_step_id"]):
            require(
                row["origin_call_id"] is None
                and row["status"] == "succeeded"
                and row["result_ref"] is not None,
                "A2A 来源调用未完整成功或混入其他Run",
            )
            identity = str(row["id"])
            intent = parse_contract(
                self.owned(row["intent_ref"], ObjectType.CALL, "external_call", identity, "intent"),
                CallIntent,
            )
            saved = parse_contract(
                self.owned(row["result_ref"], ObjectType.CALL, "external_call", identity, "result"),
                SavedCallOutcome,
            )
            record = Recording(
                intent,
                saved,
                self.owned(
                    intent.request_ref, ObjectType.BLOB, "external_call", identity, "request"
                ),
                self.owned(
                    saved.response_ref, ObjectType.BLOB, "external_call", identity, "response"
                ),
                identity,
            )
            occurrence = intent.occurrence
            require(
                (
                    occurrence.logical_step_id,
                    occurrence.call_index,
                    occurrence.attempt,
                    occurrence.provider,
                    occurrence.signature_digest,
                    occurrence.request_digest,
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
                "A2A 来源物理调用与确切意图不符",
            )
            rule = policy.rule(occurrence, intent.effect)
            if not rule.preauthorized:
                selected = [a for a in approvals if a["consumed_call_id"] == row["id"]]
                require(len(selected) == 1, "A2A 来源写调用缺少唯一消费审批")
                approval = selected[0]
                action = BoundAction(
                    repository_id=str(self.repo),
                    run_id=str(self.run_id),
                    branch_name=run["branch_name"],
                    checkpoint=intent.checkpoint,
                    occurrence=occurrence,
                    effect=intent.effect,
                    policy_digest=policy.digest(),
                    actor=self.actor,
                    max_response_bytes=intent.max_response_bytes,
                )
                require(
                    approval["status"] == "consumed"
                    and approval["decision"] == "approve"
                    and approval["actor"] == self.actor
                    and approval["checkpoint_commit_id"] == intent.checkpoint
                    and approval["action_ref"] == approval["call_digest"] == action.digest()
                    and approval["policy_digest"] == policy.digest(),
                    "A2A 来源审批绑定不符",
                )
                for ref, kind, role, raw in (
                    (
                        action.digest(),
                        ObjectType.CALL,
                        "action",
                        encode(action.model_dump(mode="json")),
                    ),
                    (
                        policy.digest(),
                        ObjectType.BLOB,
                        "policy",
                        encode(policy.model_dump(mode="json")),
                    ),
                    (intent.request_ref, ObjectType.BLOB, "request", record.request),
                ):
                    require(
                        self.owned(ref, kind, "approval", str(approval["id"]), role) == raw,
                        "A2A 来源审批内容不符",
                    )
            recordings.append(record)
        source = A2ASource(
            self.spec, program, payload.program_ref, tuple(frames), tuple(recordings), self.actor
        )
        A2ARecordedEngine(source).replay()
        return source
