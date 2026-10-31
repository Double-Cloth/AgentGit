"""实际Live来源闭包与逐检查点纯重算；不创建新的Recorded任务。"""

import copy
from dataclasses import replace
from uuid import UUID

import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from test_a2a_fixture import server as server
from test_a2a_runs import ACTOR, approve, case, ready_step, start

from agentgit.core.a2a_runs import A2ARecordedSpec
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.research import SavedCallOutcome
from agentgit.replay.a2a import A2ARecordedEngine
from agentgit.services.a2a_recordings import A2ARecordingReader, _Reader
from agentgit.services.history import TABLES

pytestmark = pytest.mark.integration


def source_case(history, server, tmp_path, mode="success"):
    values = case(history, server, tmp_path, messages=2 if mode == "input_required" else 1)
    values[5]("working" if mode == "poll" else mode)
    result = start(values)
    if mode == "poll":
        assert result["status"] == "queued"
        values[5]("success")
        result = ready_step(values)
    elif mode == "input_required":
        assert result["status"] == "queued"
        assert ready_step(values)["status"] == "waiting_approval"
        approve(values)
        values[5]("success")
        result = ready_step(values)
    assert result["status"] == "completed"
    with history.engine.connect() as c:
        links = TABLES["run_commits"]
        commits = list(
            c.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == UUID(values[4].run_id))
                .order_by(links.c.run_order)
            ).scalars()
        )
    spec = A2ARecordedSpec(
        source_run_id=values[4].run_id, from_commit=commits[0], to_commit=commits[-1]
    )
    return values, spec


def fingerprint(history):
    with history.engine.connect() as c:
        return {
            name: sorted(repr(dict(row)) for row in c.execute(sa.select(table)).mappings())
            for name, table in TABLES.items()
        }


def forbidden(*args, **kwargs):
    pytest.fail("Recorded 读取／重算尝试新写入或外部依赖")


@pytest.mark.parametrize("mode", ["success", "structured", "poll", "input_required"])
def test_complete_source_recomputes_all_state_artifact_hashes_without_writes_or_io(
    history, server, tmp_path, monkeypatch, mode
):
    values, spec = source_case(history, server, tmp_path, mode)
    before, requests = fingerprint(history), copy.deepcopy(values[6]())
    files = {
        p.relative_to(history.objects.root).as_posix(): p.read_bytes()
        for p in history.objects.root.rglob("*")
        if p.is_file()
    }
    with monkeypatch.context() as patch:
        patch.setattr(history.objects, "put", forbidden)
        source = A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
        patch.setattr(history.objects, "get", forbidden)
        patch.setattr(history.engine, "connect", forbidden)
        patch.setattr("agentgit.adapters.a2a.configured_client", forbidden)
        patch.setattr("socket.create_connection", forbidden)
        frames = A2ARecordedEngine(source).replay()
    assert len(frames) == 1 + 2 * len(source.recordings)
    assert len(source.recordings) == (2 if mode in {"poll", "input_required"} else 1)
    for actual, expected in zip(frames, source.frames, strict=True):
        assert actual.snapshot == expected.snapshot
        assert (
            object_digest(ObjectType.STATE, encode(actual.snapshot.model_dump(mode="json")))
            == expected.state_ref
        )
    assert fingerprint(history) == before
    assert values[6]() == requests
    assert files == {
        p.relative_to(history.objects.root).as_posix(): p.read_bytes()
        for p in history.objects.root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "change", ["actor", "repository", "partial", "non_live", "unfinished", "old_source", "old_lock"]
)
def test_source_scope_environment_and_complete_range_are_strict(history, server, tmp_path, change):
    values, spec = source_case(history, server, tmp_path)
    repo, actor = values[2], ACTOR
    table, key, original = None, None, None
    if change == "actor":
        actor = "another-operator"
    elif change == "repository":
        repo = history.create_repository("另一个项目")
    elif change == "partial":
        spec = spec.model_copy(update={"from_commit": spec.to_commit})
    elif change in {"non_live", "unfinished"}:
        table = TABLES["runs"]
        key = table.c.id == UUID(spec.source_run_id)
        original = {"mode": "live"} if change == "non_live" else {"status": "completed"}
        with history.engine.begin() as c:
            c.execute(
                table.update()
                .where(key)
                .values(**({"mode": "recorded"} if change == "non_live" else {"status": "failed"}))
            )
    else:
        table = TABLES["jobs"]
        key = table.c.id == UUID(values[4].job_id)
        with history.engine.begin() as c:
            job = dict(c.execute(sa.select(table).where(key)).mappings().one())
            original = {"payload_ref": job["payload_ref"]}
            payload = values[1].payload(repo, job["id"]).model_dump(mode="json")
            payload["code_revision" if change == "old_source" else "dependency_lock_digest"] = (
                "source-sha256:" + "0" * 64 if change == "old_source" else "0" * 64
            )
            obj = history.objects.put(ObjectType.BLOB, encode(payload))
            history._ref(c, repo, obj, "job", str(job["id"]), "payload")
            c.execute(table.update().where(key).values(payload_ref=obj.id))
    try:
        with pytest.raises(AgentGitError) as error:
            A2ARecordingReader(history).inspect(repo, spec, actor=actor)
        assert error.value.code in {
            ErrorCode.FORBIDDEN,
            ErrorCode.NOT_FOUND,
            ErrorCode.REPLAY_MISMATCH,
            ErrorCode.UNSUPPORTED_SCHEMA,
        }
    finally:
        if table is not None:
            with history.engine.begin() as c:
                c.execute(table.update().where(key).values(**original))


@pytest.mark.parametrize(
    "change",
    [
        "call_index",
        "unknown_call",
        "response_ref",
        "snapshot_index",
        "parent_order",
        "event_type",
        "artifact_index",
        "approval",
        "missing_reference",
    ],
)
def test_actual_relation_tampering_is_rejected(history, server, tmp_path, monkeypatch, change):
    values, spec = source_case(history, server, tmp_path)
    immutable = change in {"snapshot_index", "parent_order", "event_type", "artifact_index"}
    with history.engine.begin() as c:
        if change in {"call_index", "unknown_call", "response_ref"}:
            table = TABLES["external_calls"]
            row = dict(c.execute(sa.select(table)).mappings().one())
            key = table.c.id == row["id"]
            updates = (
                {"call_index": 1}
                if change == "call_index"
                else {"status": "outcome_unknown"}
                if change == "unknown_call"
                else {"result_ref": None}
            )
        elif change == "snapshot_index":
            table = TABLES["snapshots"]
            row = dict(
                c.execute(sa.select(table).where(table.c.commit_id == spec.to_commit))
                .mappings()
                .one()
            )
            key, updates = (
                table.c.commit_id == row["commit_id"],
                {"event_seq": row["event_seq"] - 1},
            )
        elif change == "parent_order":
            table = TABLES["commit_parents"]
            row = dict(
                c.execute(sa.select(table).where(table.c.commit_id == spec.to_commit))
                .mappings()
                .one()
            )
            key, updates = table.c.commit_id == row["commit_id"], {"parent_order": 1}
        elif change == "event_type":
            table = TABLES["events"]
            row = dict(
                c.execute(sa.select(table).where(table.c.type == "a2a.observed")).mappings().one()
            )
            key, updates = table.c.seq == row["seq"], {"type": "a2a.unknown"}
        elif change == "artifact_index":
            table = TABLES["artifacts"]
            row = dict(
                c.execute(sa.select(table).where(table.c.commit_id == spec.to_commit).limit(1))
                .mappings()
                .one()
            )
            key, updates = table.c.id == row["id"], {"relative_path": "a2a/forged.json"}
        elif change == "approval":
            table = TABLES["approvals"]
            row = dict(c.execute(sa.select(table)).mappings().one())
            key, updates = table.c.id == row["id"], {"actor": "forged-operator"}
        else:
            table = TABLES["external_calls"]
            row = dict(c.execute(sa.select(table)).mappings().one())
            key = table.c.id == row["id"]
            response = history.objects.put(ObjectType.BLOB, b'{"unregistered":true}')
            history._index(c, response)
            saved = SavedCallOutcome(
                response_ref=response.id, response_bytes=response.byte_length, complete=True
            )
            item = history.objects.put(ObjectType.CALL, encode(saved.model_dump(mode="json")))
            history._ref(c, values[2], item, "external_call", str(row["id"]), "result")
            updates = {"result_ref": item.id}
        if immutable:
            # 原数据库保护必须保持启用；损坏读取视图另行注入，不禁用触发器。
            with (
                pytest.raises(sa.exc.ProgrammingError, match="immutable_history"),
                c.begin_nested(),
            ):
                c.execute(table.update().where(key).values(**updates))
        else:
            c.execute(table.update().where(key).values(**updates))
    if immutable:
        original_rows = _Reader.rows
        primary = tuple(table.primary_key.columns.keys())

        def damaged_rows(reader, name, *conditions, limit=1025):
            rows = original_rows(reader, name, *conditions, limit=limit)
            if name == table.name:
                for candidate in rows:
                    if all(candidate[k] == row[k] for k in primary):
                        candidate.update(updates)
            return rows

        with monkeypatch.context() as patch:
            patch.setattr(_Reader, "rows", damaged_rows)
            with pytest.raises(AgentGitError) as error:
                A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
            assert error.value.code == ErrorCode.REPLAY_MISMATCH
        A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
        return
    try:
        with pytest.raises(AgentGitError) as error:
            A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
        assert error.value.code in {ErrorCode.RECORDING_MISSING, ErrorCode.REPLAY_MISMATCH}
    finally:
        with history.engine.begin() as c:
            c.execute(table.update().where(key).values(**{name: row[name] for name in updates}))


@pytest.mark.parametrize(
    "change",
    [
        "missing_record",
        "duplicate_record",
        "checkpoint",
        "request",
        "response",
        "incomplete",
        "state",
        "artifact",
        "event",
        "program",
    ],
)
def test_pure_engine_rejects_missing_or_changed_dependencies(
    history, server, tmp_path, monkeypatch, change
):
    values, spec = source_case(history, server, tmp_path)
    source = A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
    record = source.recordings[0]
    if change == "missing_record":
        source = replace(source, recordings=())
    elif change == "duplicate_record":
        source = replace(source, recordings=(record, record))
    elif change == "checkpoint":
        source = replace(
            source,
            recordings=(
                replace(
                    record, intent=record.intent.model_copy(update={"checkpoint": spec.from_commit})
                ),
            ),
        )
    elif change in {"request", "response"}:
        source = replace(source, recordings=(replace(record, **{change: b"{}"}),))
    elif change == "incomplete":
        source = replace(
            source,
            recordings=(
                replace(record, saved=record.saved.model_copy(update={"complete": False})),
            ),
        )
    elif change in {"state", "artifact", "event"}:
        frame = source.frames[-1]
        if change == "state":
            data = copy.deepcopy(frame.snapshot.data)
            data["usage"]["tool_calls"] += 1
            frame = replace(frame, snapshot=frame.snapshot.model_copy(update={"data": data}))
        elif change == "artifact":
            frame = replace(frame, snapshot=frame.snapshot.model_copy(update={"artifacts": ()}))
        else:
            frame = replace(frame, event="a2a.control_applied")
        source = replace(source, frames=(*source.frames[:-1], frame))
    else:
        source = replace(source, program=source.program.model_copy(update={"max_polls": 1}))
    with monkeypatch.context() as patch:
        patch.setattr(history.engine, "connect", forbidden)
        patch.setattr(history.objects, "get", forbidden)
        with pytest.raises(AgentGitError) as error:
            A2ARecordedEngine(source).replay()
        assert error.value.code in {ErrorCode.REPLAY_MISMATCH, ErrorCode.OBJECT_CORRUPT}


def test_missing_physical_response_fails_without_live_fallback(history, server, tmp_path):
    values, spec = source_case(history, server, tmp_path)
    source = A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
    path = history.objects.path_for(source.recordings[0].saved.response_ref)
    raw = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(AgentGitError) as error:
            A2ARecordingReader(history).inspect(values[2], spec, actor=ACTOR)
        assert error.value.code == ErrorCode.NOT_FOUND
    finally:
        path.write_bytes(raw)
