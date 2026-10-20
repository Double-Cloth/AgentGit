"""S2 原子版本提交；外部调用不进入本服务的数据库事务。"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CommitManifest, EnvironmentManifest, EventRange, parse_contract
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.snapshots import (
    ArtifactEntry,
    EventManifest,
    EventPayload,
    LogicalEvent,
    StateSnapshot,
)
from agentgit.storage.objects import FaultHook, ObjectStore, StoredObject
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


@dataclass(frozen=True)
class EventInput:
    type: str
    logical_step_id: str
    payload: dict[str, Any]
    caused_by_seq: int | None = None
    caused_by_run_id: UUID | None = None


@dataclass(frozen=True)
class ArtifactInput:
    path: str
    content: bytes
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class CommitResult:
    commit_id: str
    state_ref: str
    branch_version: int
    last_event_seq: int


def _error(code: ErrorCode, message: str) -> AgentGitError:
    return AgentGitError(code, message)


class HistoryService:
    def __init__(
        self,
        engine: Engine,
        objects: ObjectStore,
        *,
        fault: FaultHook | None = None,
        lease_guard: Callable[[Connection, UUID], None] | None = None,
    ):
        self.engine = engine
        self.objects = objects
        self.fault = fault or (lambda _: None)
        self.lease_guard = lease_guard

    def create_repository(self, name: str) -> UUID:
        if not name.strip() or len(name) > 200:
            raise _error(ErrorCode.VALIDATION, "项目名称无效")
        repository_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(TABLES["repositories"].insert().values(id=repository_id, name=name))
            connection.execute(
                TABLES["branches"].insert().values(repository_id=repository_id, name="main")
            )
        return repository_id

    def branch(self, repository_id: UUID, name: str = "main") -> dict[str, Any]:
        table = TABLES["branches"]
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository_id, table.c.name == name
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise _error(ErrorCode.NOT_FOUND, "分支不可访问")
        return dict(row)

    def create_run(self, repository_id: UUID, branch_name: str = "main") -> UUID:
        run_id = uuid4()
        with self.engine.begin() as connection:
            branches = TABLES["branches"]
            branch = (
                connection.execute(
                    sa.select(branches)
                    .where(
                        branches.c.repository_id == repository_id, branches.c.name == branch_name
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if branch is None:
                raise _error(ErrorCode.NOT_FOUND, "分支不可访问")
            if branch["active_writer_run_id"] is not None:
                raise _error(ErrorCode.VERSION_CONFLICT, "分支已有活动写入 Run")
            connection.execute(
                TABLES["runs"]
                .insert()
                .values(
                    id=run_id,
                    repository_id=repository_id,
                    branch_name=branch_name,
                    base_commit_id=branch["head_commit_id"],
                    checkpoint_commit_id=branch["head_commit_id"],
                    mode="live",
                    status="running",
                )
            )
        return run_id

    def register_input(self, repository_id: UUID, payload: bytes) -> str:
        item = self.objects.put(ObjectType.BLOB, payload)
        with self.engine.begin() as connection:
            repo = TABLES["repositories"]
            if (
                connection.execute(sa.select(repo.c.id).where(repo.c.id == repository_id)).first()
                is None
            ):
                raise _error(ErrorCode.NOT_FOUND, "项目不可访问")
            self._ref(connection, repository_id, item, "repository", str(repository_id), "input")
        return item.id

    def _run(
        self, connection: Connection, repository_id: UUID, run_id: UUID, *, lock: bool = False
    ) -> RowMapping:
        table = TABLES["runs"]
        query = sa.select(table).where(table.c.repository_id == repository_id, table.c.id == run_id)
        row = connection.execute(query.with_for_update() if lock else query).mappings().first()
        if row is None:
            raise _error(ErrorCode.NOT_FOUND, "运行不可访问")
        return row

    def _index(self, connection: Connection, item: StoredObject) -> None:
        values = asdict(item)
        values["object_type"] = item.object_type.value
        connection.execute(insert(TABLES["objects"]).values(**values).on_conflict_do_nothing())
        table = TABLES["objects"]
        row = connection.execute(sa.select(table).where(table.c.id == item.id)).mappings().one()
        if any(
            row[key] != values[key]
            for key in ("object_type", "codec", "byte_length", "storage_key")
        ):
            raise _error(ErrorCode.OBJECT_CORRUPT, "对象索引与内容不符")

    def assert_writer(self, connection: Connection, repository_id: UUID, run_id: UUID) -> None:
        run = self._run(connection, repository_id, run_id)
        branches = TABLES["branches"]
        branch = connection.execute(
            sa.select(branches.c.active_writer_run_id)
            .where(branches.c.repository_id == repository_id, branches.c.name == run["branch_name"])
            .with_for_update()
        ).scalar_one()
        if branch != run_id:
            raise _error(ErrorCode.VERSION_CONFLICT, "Run 已失去分支写入绑定")
        jobs = TABLES["jobs"]
        managed = connection.execute(sa.select(jobs.c.id).where(jobs.c.run_id == run_id)).first()
        if self.lease_guard is None and managed is not None:
            raise _error(ErrorCode.VERSION_CONFLICT, "受调度的 Run 必须提供有效租约上下文")
        if self.lease_guard is not None:
            self.lease_guard(connection, run_id)

    def _ref(
        self,
        connection: Connection,
        repository_id: UUID,
        item: StoredObject,
        owner_type: str,
        owner_id: str,
        role: str,
    ) -> None:
        self._index(connection, item)
        connection.execute(
            insert(TABLES["object_refs"])
            .values(
                repository_id=repository_id,
                object_id=item.id,
                owner_type=owner_type,
                owner_id=owner_id,
                role=role,
            )
            .on_conflict_do_nothing()
        )

    def _event(
        self,
        connection: Connection,
        repository_id: UUID,
        run_id: UUID,
        seq: int,
        event: EventInput,
        item: StoredObject,
    ) -> None:
        if not event.type or not event.logical_step_id:
            raise _error(ErrorCode.VALIDATION, "事件类型与逻辑步骤不能为空")
        cause_run = event.caused_by_run_id or (run_id if event.caused_by_seq is not None else None)
        if cause_run is not None:
            table = TABLES["events"]
            cause = connection.execute(
                sa.select(table.c.seq).where(
                    table.c.repository_id == repository_id,
                    table.c.run_id == cause_run,
                    table.c.seq == event.caused_by_seq,
                )
            ).first()
            if cause is None or (
                cause_run == run_id
                and event.caused_by_seq is not None
                and event.caused_by_seq >= seq
            ):
                raise _error(ErrorCode.VALIDATION, "因果事件不可访问或指向未来")
        self._ref(connection, repository_id, item, "event", f"{run_id}:{seq}", "payload")
        connection.execute(
            TABLES["events"]
            .insert()
            .values(
                repository_id=repository_id,
                run_id=run_id,
                seq=seq,
                type=event.type,
                logical_step_id=event.logical_step_id,
                payload_ref=item.id,
                caused_by_run_id=cause_run,
                caused_by_seq=event.caused_by_seq,
            )
        )

    def append_event(self, repository_id: UUID, run_id: UUID, event: EventInput) -> int:
        encode(event.payload)
        item = self.objects.put_json(
            ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
        )
        with self.engine.begin() as connection:
            run = self._run(connection, repository_id, run_id, lock=True)
            self.assert_writer(connection, repository_id, run_id)
            if run["status"] not in ("running", "planning"):
                raise _error(ErrorCode.VERSION_CONFLICT, "运行当前不能追加执行事件")
            seq = int(run["last_event_seq"]) + 1
            self._event(connection, repository_id, run_id, seq, event, item)
            connection.execute(
                TABLES["runs"]
                .update()
                .where(TABLES["runs"].c.id == run_id)
                .values(last_event_seq=seq)
            )
        return seq

    def commit(
        self,
        repository_id: UUID,
        run_id: UUID,
        *,
        expected_head: str | None,
        expected_version: int,
        state: dict[str, Any],
        environment: EnvironmentManifest,
        events: tuple[EventInput, ...],
        artifacts: tuple[ArtifactInput, ...] = (),
        writer_guard: Callable[[Connection], None] | None = None,
        projection: Callable[[Connection], None] | None = None,
    ) -> CommitResult:
        if not events:
            raise _error(ErrorCode.VALIDATION, "状态转移至少需要一个事件")
        encode(state)
        for event in events:
            encode(event.payload)
        validate_artifact_paths(item.path for item in artifacts)
        # 读出序号后先发布所有对象；短事务内再次核对序号及 HEAD，竞争只产生孤儿。
        with self.engine.connect() as connection:
            observed = self._run(connection, repository_id, run_id)
            if observed["checkpoint_commit_id"] != expected_head:
                raise _error(ErrorCode.VERSION_CONFLICT, "运行 checkpoint 与预期 HEAD 不符")
            if expected_head is not None:
                self._commit_row(connection, repository_id, expected_head)
            pending = self._event_rows(
                connection, repository_id, run_id, after=int(observed["checkpoint_event_seq"])
            )
            refs = TABLES["object_refs"]
            if (
                connection.execute(
                    sa.select(refs.c.object_id).where(
                        refs.c.repository_id == repository_id,
                        refs.c.object_id == environment.input_ref,
                    )
                ).first()
                is None
            ):
                raise _error(ErrorCode.NOT_FOUND, "环境输入未在当前项目登记")
        self.objects.get(environment.input_ref, ObjectType.BLOB)
        branch = self.branch(repository_id, observed["branch_name"])
        if branch["head_commit_id"] != expected_head or branch["version"] != expected_version:
            raise _error(ErrorCode.VERSION_CONFLICT, "分支 HEAD 或版本已改变")
        first_seq = int(observed["checkpoint_event_seq"]) + 1
        last_observed = int(observed["last_event_seq"])
        artifact_objects = [
            (item, self.objects.put(ObjectType.BLOB, item.content)) for item in artifacts
        ]
        snapshot = StateSnapshot(
            data=state,
            artifacts=tuple(
                ArtifactEntry(path=item.path, object_ref=obj.id, media_type=item.media_type)
                for item, obj in sorted(artifact_objects, key=lambda pair: pair[0].path)
            ),
        )
        state_obj = self.objects.put_json(ObjectType.STATE, snapshot.model_dump(mode="json"))
        environment_obj = self.objects.put_json(
            ObjectType.ENVIRONMENT, environment.model_dump(mode="json")
        )
        event_objects = [
            self.objects.put_json(
                ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
            )
            for event in events
        ]
        event_records = [self._logical_event(row, run_id) for row in pending]
        for index, (event, obj) in enumerate(
            zip(events, event_objects, strict=True), last_observed + 1
        ):
            event_records.append(
                {
                    "seq": index,
                    "type": event.type,
                    "logical_step_id": event.logical_step_id,
                    "payload_ref": obj.id,
                    "cause": self._logical_cause(
                        event.caused_by_run_id or run_id, event.caused_by_seq, run_id
                    ),
                }
            )
        event_manifest = EventManifest(
            events=tuple(LogicalEvent(**record) for record in event_records)
        )
        event_obj = self.objects.put_json(
            ObjectType.EVENT_MANIFEST, event_manifest.model_dump(mode="json")
        )
        last_seq = last_observed + len(events)
        manifest = CommitManifest(
            repository_id=str(repository_id),
            parents=(expected_head,) if expected_head else (),
            state_ref=state_obj.id,
            environment_ref=environment_obj.id,
            event_manifest_ref=event_obj.id,
            event_range=EventRange(first_seq=first_seq, last_seq=last_seq),
        )
        manifest_obj = self.objects.put_json(ObjectType.COMMIT, manifest.model_dump(mode="json"))
        self.fault("before_transaction")
        try:
            with self.engine.begin() as connection:
                run = self._run(connection, repository_id, run_id, lock=True)
                branches = TABLES["branches"]
                locked_branch = (
                    connection.execute(
                        sa.select(branches)
                        .where(
                            branches.c.repository_id == repository_id,
                            branches.c.name == run["branch_name"],
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if (
                    locked_branch["head_commit_id"] != expected_head
                    or locked_branch["version"] != expected_version
                ):
                    raise _error(ErrorCode.VERSION_CONFLICT, "分支 HEAD 或版本已改变")
                if (
                    run["last_event_seq"] != last_observed
                    or run["checkpoint_commit_id"] != expected_head
                ):
                    raise _error(ErrorCode.VERSION_CONFLICT, "运行在对象发布期间已改变")
                if run["status"] not in (
                    "queued",
                    "running",
                    "planning",
                    "paused",
                    "waiting_approval",
                    "outcome_unknown",
                ):
                    raise _error(ErrorCode.VERSION_CONFLICT, "运行当前不能提交状态")
                if run["status"] == "outcome_unknown" and self.lease_guard is None:
                    raise _error(ErrorCode.VERSION_CONFLICT, "未知结果必须经受租约保护的核对恢复")
                if writer_guard is not None:
                    writer_guard(connection)
                self.assert_writer(connection, repository_id, run_id)
                all_objects = [state_obj, environment_obj, event_obj, manifest_obj, *event_objects]
                all_objects.extend(obj for _, obj in artifact_objects)
                for obj in sorted(
                    {item.id: item for item in all_objects}.values(), key=lambda item: item.id
                ):
                    self._index(connection, obj)
                for role, obj in (
                    ("state", state_obj),
                    ("environment", environment_obj),
                    ("events", event_obj),
                    ("manifest", manifest_obj),
                ):
                    self._ref(connection, repository_id, obj, "commit", manifest_obj.id, role)
                connection.execute(
                    insert(TABLES["commits"])
                    .values(
                        id=manifest_obj.id,
                        repository_id=repository_id,
                        state_ref=state_obj.id,
                        environment_ref=environment_obj.id,
                        event_manifest_ref=event_obj.id,
                    )
                    .on_conflict_do_nothing()
                )
                if expected_head is not None:
                    connection.execute(
                        insert(TABLES["commit_parents"])
                        .values(
                            repository_id=repository_id,
                            commit_id=manifest_obj.id,
                            parent_order=0,
                            parent_id=expected_head,
                        )
                        .on_conflict_do_nothing()
                    )
                for seq, (event, obj) in enumerate(
                    zip(events, event_objects, strict=True), last_observed + 1
                ):
                    self._event(connection, repository_id, run_id, seq, event, obj)
                for item, obj in artifact_objects:
                    artifact_id = uuid4()
                    self._ref(
                        connection,
                        repository_id,
                        obj,
                        "commit",
                        manifest_obj.id,
                        f"artifact:{item.path}",
                    )
                    connection.execute(
                        insert(TABLES["artifacts"])
                        .values(
                            id=artifact_id,
                            repository_id=repository_id,
                            commit_id=manifest_obj.id,
                            relative_path=item.path,
                            object_id=obj.id,
                            media_type=item.media_type,
                        )
                        .on_conflict_do_nothing(index_elements=["commit_id", "relative_path"])
                    )
                links = TABLES["run_commits"]
                run_order = (
                    connection.execute(
                        sa.select(sa.func.count())
                        .select_from(links)
                        .where(links.c.run_id == run_id)
                    ).scalar_one()
                    + 1
                )
                connection.execute(
                    links.insert().values(
                        repository_id=repository_id,
                        run_id=run_id,
                        run_order=run_order,
                        commit_id=manifest_obj.id,
                    )
                )
                connection.execute(
                    TABLES["snapshots"]
                    .insert()
                    .values(
                        repository_id=repository_id,
                        run_id=run_id,
                        commit_id=manifest_obj.id,
                        event_seq=last_seq,
                        state_ref=state_obj.id,
                    )
                )
                branches = TABLES["branches"]
                updated = connection.execute(
                    branches.update()
                    .where(
                        branches.c.repository_id == repository_id,
                        branches.c.name == run["branch_name"],
                        branches.c.head_commit_id.is_not_distinct_from(expected_head),
                        branches.c.version == expected_version,
                    )
                    .values(head_commit_id=manifest_obj.id, version=expected_version + 1)
                    .returning(branches.c.version)
                ).first()
                if updated is None:
                    raise _error(ErrorCode.VERSION_CONFLICT, "分支 HEAD 或版本已改变")
                connection.execute(
                    TABLES["runs"]
                    .update()
                    .where(TABLES["runs"].c.id == run_id)
                    .values(
                        last_event_seq=last_seq,
                        checkpoint_event_seq=last_seq,
                        checkpoint_commit_id=manifest_obj.id,
                    )
                )
                if projection is not None:
                    projection(connection)
                if self.lease_guard is not None:
                    connection.execute(
                        TABLES["jobs"]
                        .update()
                        .where(TABLES["jobs"].c.run_id == run_id)
                        .values(checkpoint_commit_id=manifest_obj.id)
                    )
                    self.lease_guard(connection, run_id)
                self.fault("in_transaction")
        except IntegrityError:
            raise _error(ErrorCode.VALIDATION, "提交违反归属或历史约束") from None
        self.fault("after_commit")
        return CommitResult(manifest_obj.id, state_obj.id, expected_version + 1, last_seq)

    @staticmethod
    def _logical_cause(cause_run: UUID, seq: int | None, run_id: UUID) -> dict[str, Any] | None:
        return (
            None
            if seq is None
            else {"run": "self" if cause_run == run_id else str(cause_run), "seq": seq}
        )

    def _logical_event(self, row: RowMapping, run_id: UUID) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "type": row["type"],
            "logical_step_id": row["logical_step_id"],
            "payload_ref": row["payload_ref"],
            "cause": self._logical_cause(
                row["caused_by_run_id"] or run_id, row["caused_by_seq"], run_id
            ),
        }

    def _event_rows(
        self,
        connection: Connection,
        repository_id: UUID,
        run_id: UUID,
        *,
        after: int = 0,
        limit: int | None = None,
    ) -> list[RowMapping]:
        table = TABLES["events"]
        query = (
            sa.select(table)
            .where(
                table.c.repository_id == repository_id,
                table.c.run_id == run_id,
                table.c.seq > after,
            )
            .order_by(table.c.seq)
        )
        if limit is not None:
            query = query.limit(limit)
        return list(connection.execute(query).mappings())

    def events(
        self, repository_id: UUID, run_id: UUID, *, after: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise _error(ErrorCode.VALIDATION, "事件分页参数越界")
        with self.engine.connect() as connection:
            self._run(connection, repository_id, run_id)
            return [
                dict(row)
                for row in self._event_rows(
                    connection, repository_id, run_id, after=after, limit=limit
                )
            ]

    def _commit_row(
        self, connection: Connection, repository_id: UUID, commit_id: str
    ) -> RowMapping:
        table = TABLES["commits"]
        row = (
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repository_id, table.c.id == commit_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise _error(ErrorCode.NOT_FOUND, "Commit 不可访问")
        return row

    def restore(self, repository_id: UUID, commit_id: str) -> StateSnapshot:
        with self.engine.connect() as connection:
            row = self._commit_row(connection, repository_id, commit_id)
            parents = TABLES["commit_parents"]
            parent_ids = tuple(
                connection.execute(
                    sa.select(parents.c.parent_id)
                    .where(
                        parents.c.repository_id == repository_id, parents.c.commit_id == commit_id
                    )
                    .order_by(parents.c.parent_order)
                ).scalars()
            )
        manifest = parse_contract(self.objects.get(commit_id, ObjectType.COMMIT), CommitManifest)
        if manifest.repository_id != str(repository_id) or any(
            getattr(manifest, key) != row[key]
            for key in ("state_ref", "environment_ref", "event_manifest_ref")
        ):
            raise _error(ErrorCode.OBJECT_CORRUPT, "Commit 索引与 manifest 不符")
        if manifest.parents != parent_ids:
            raise _error(ErrorCode.OBJECT_CORRUPT, "Commit 父关系与 manifest 不符")
        environment = parse_contract(
            self.objects.get(row["environment_ref"], ObjectType.ENVIRONMENT), EnvironmentManifest
        )
        self.objects.get(environment.input_ref, ObjectType.BLOB)
        event_manifest = parse_contract(
            self.objects.get(row["event_manifest_ref"], ObjectType.EVENT_MANIFEST), EventManifest
        )
        expected_count = manifest.event_range.last_seq - manifest.event_range.first_seq + 1
        if len(event_manifest.events) != expected_count:
            raise _error(ErrorCode.OBJECT_CORRUPT, "事件清单与范围不符")
        for seq, event in enumerate(event_manifest.events, manifest.event_range.first_seq):
            if event.seq != seq:
                raise _error(ErrorCode.OBJECT_CORRUPT, "事件清单序号不连续")
            parse_contract(self.objects.get(event.payload_ref, ObjectType.EVENT), EventPayload)
        snapshot = parse_contract(
            self.objects.get(row["state_ref"], ObjectType.STATE), StateSnapshot
        )
        for artifact in snapshot.artifacts:
            self.objects.get(artifact.object_ref, ObjectType.BLOB)
        validate_artifact_paths(item.path for item in snapshot.artifacts)
        indexed = [
            (item["relative_path"], item["object_id"], item["media_type"])
            for item in self.artifacts(repository_id, commit_id)
        ]
        expected = [(item.path, item.object_ref, item.media_type) for item in snapshot.artifacts]
        if indexed != expected:
            raise _error(ErrorCode.OBJECT_CORRUPT, "产物索引与状态 manifest 不符")
        return snapshot

    def log(self, repository_id: UUID, *, branch_name: str = "main", limit: int = 100) -> list[str]:
        if not 1 <= limit <= 1000:
            raise _error(ErrorCode.VALIDATION, "历史分页大小越界")
        current = self.branch(repository_id, branch_name)["head_commit_id"]
        result: list[str] = []
        with self.engine.connect() as connection:
            while current is not None and len(result) < limit:
                self._commit_row(connection, repository_id, current)
                if current in result:
                    raise _error(ErrorCode.OBJECT_CORRUPT, "历史包含循环")
                result.append(current)
                parents = TABLES["commit_parents"]
                current = connection.execute(
                    sa.select(parents.c.parent_id)
                    .where(parents.c.repository_id == repository_id, parents.c.commit_id == current)
                    .order_by(parents.c.parent_order)
                ).scalar_one_or_none()
        return result

    def artifacts(self, repository_id: UUID, commit_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._commit_row(connection, repository_id, commit_id)
            table = TABLES["artifacts"]
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(table)
                    .where(table.c.repository_id == repository_id, table.c.commit_id == commit_id)
                    .order_by(table.c.relative_path)
                ).mappings()
            ]

    def read_artifact(self, repository_id: UUID, artifact_id: UUID) -> bytes:
        table = TABLES["artifacts"]
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository_id, table.c.id == artifact_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise _error(ErrorCode.NOT_FOUND, "产物不可访问")
        return self.objects.get(row["object_id"], ObjectType.BLOB)

    def gc_dry_run(self) -> dict[str, list[str]]:
        # 所有已入索引对象保守保留，包含 Run、调用与未来审批预留引用；S2 无删除接口。
        with self.engine.connect() as connection:
            indexed = set(connection.execute(sa.select(TABLES["objects"].c.id)).scalars())
        orphan: list[str] = []
        temporary: list[str] = []
        for key, path in self.objects.inventory():
            if key == "temporary":
                temporary.append(str(path.relative_to(self.objects.root)))
            elif key not in indexed:
                orphan.append(key)
        return {
            "retained": sorted(indexed),
            "unindexed": sorted(orphan),
            "temporary": sorted(temporary),
        }
