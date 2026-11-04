"""在独立测试库销毁前保存有界证据包；只用于固定合成样例。"""

import base64
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.services.history import TABLES
from agentgit.services.management import public

MAX_INDEX_ROWS = 20000
MAX_OBJECTS = 4000
MAX_BYTES = 16 * 1024 * 1024


def pack_repository_evidence(history, result):
    repository = UUID(result["repository_id"])
    indexes = {}
    with history.engine.connect() as connection:
        for identity in (result["reference_run_id"], result["failed_run_id"]):
            history._run(connection, repository, UUID(identity))
        for name in (
            "runs",
            "events",
            "commits",
            "commit_parents",
            "run_commits",
            "snapshots",
            "external_calls",
            "object_refs",
        ):
            table = TABLES[name]
            rows = list(
                connection.execute(
                    sa.select(table)
                    .where(table.c.repository_id == repository)
                    .limit(MAX_INDEX_ROWS + 1)
                ).mappings()
            )
            assert len(rows) <= MAX_INDEX_ROWS, "样例所属索引超过归档上限"
            indexes[name] = sorted((public(dict(row)) for row in rows), key=encode)
        objects, refs = TABLES["objects"], TABLES["object_refs"]
        rows = list(
            connection.execute(
                sa.select(objects)
                .join(refs, refs.c.object_id == objects.c.id)
                .where(refs.c.repository_id == repository)
                .distinct()
                .order_by(objects.c.id)
                .limit(MAX_OBJECTS + 1)
            ).mappings()
        )
    assert len(rows) <= MAX_OBJECTS, "样例对象超过归档上限"
    contents = {}
    total = 0
    for row in rows:
        kind = ObjectType(row["object_type"])
        payload = history.objects.get(row["id"], kind)
        assert len(payload) == row["byte_length"]
        total += len(payload)
        assert total <= MAX_BYTES, "样例对象累计超过归档字节上限"
        contents[row["id"]] = {
            "object_type": kind.value,
            "byte_length": len(payload),
            "base64": base64.b64encode(payload).decode("ascii"),
        }
    bundle = {
        "schema_version": 1,
        "kind": "diagnosis-fixture-evidence/v1",
        "test_only": True,
        "repository_id": str(repository),
        "case_id": result["case_id"],
        "indexes": indexes,
        "objects": contents,
        "raw_bytes": total,
    }
    verify_repository_evidence(bundle, result)
    return bundle


def verify_repository_evidence(bundle, result):
    assert bundle["test_only"] and bundle["repository_id"] == result["repository_id"]
    contents, indexes = bundle["objects"], bundle["indexes"]
    assert len(contents) <= MAX_OBJECTS
    total = 0
    for identity, item in contents.items():
        payload = base64.b64decode(item["base64"], validate=True)
        assert object_digest(ObjectType(item["object_type"]), payload) == identity
        assert len(payload) == item["byte_length"]
        total += len(payload)
    assert total == bundle["raw_bytes"] and total <= MAX_BYTES
    for rows in indexes.values():
        assert len(rows) <= MAX_INDEX_ROWS
        assert all(row["repository_id"] == result["repository_id"] for row in rows)
    assert all(ref["object_id"] in contents for ref in indexes["object_refs"])
    events = {(event["run_id"], event["seq"]): event for event in indexes["events"]}
    for graph in (result["graph"], result["reference_graph"]):
        for node in graph["nodes"]:
            ref = node["event"]
            event = events[(ref["run_id"], ref["seq"])]
            assert event["id"] == ref["event_id"] and event["payload_ref"] == node["payload_ref"]
            assert node["payload_ref"] in contents
            if node["occurrence"]:
                assert node["occurrence"]["request_digest"] in contents
            if node["observation_ref"]:
                assert node["observation_ref"] in contents
