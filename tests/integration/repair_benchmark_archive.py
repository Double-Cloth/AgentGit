"""有界顺序归档整例原始 CAS；避免为五方法复制大段 base64 或截掉失败记录。"""

import hashlib
import json
from pathlib import Path

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.services.management import public
from agentgit.storage.schema import build_metadata

MAX_OBJECTS = 60000
MAX_RAW_BYTES = 256 * 1024 * 1024
MAX_ROWS = 100000
MAX_INDEX_BYTES = 64 * 1024 * 1024
TABLES = build_metadata().tables


def write_case(history, repository, folder: Path, record):
    folder.mkdir(parents=True, exist_ok=False)
    indexes = {}
    with history.engine.connect() as connection:
        for name, table in TABLES.items():
            if "repository_id" not in table.c:
                continue
            rows = list(
                connection.execute(
                    sa.select(table)
                    .where(
                        table.c.repository_id == repository,
                    )
                    .limit(MAX_ROWS + 1)
                ).mappings()
            )
            assert len(rows) <= MAX_ROWS, "整例索引超过预先声明的归档上限"
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
    assert len(rows) <= MAX_OBJECTS
    entries, offset, digest = {}, 0, hashlib.sha256()
    with (folder / "objects.bin").open("xb") as handle:
        for row in rows:
            kind = ObjectType(row["object_type"])
            payload = history.objects.get(row["id"], kind)
            assert len(payload) == row["byte_length"] and object_digest(kind, payload) == row["id"]
            assert offset + len(payload) <= MAX_RAW_BYTES, "整例 CAS 超过预先声明的归档上限"
            entries[row["id"]] = dict(
                object_type=kind.value, byte_length=len(payload), offset=offset
            )
            handle.write(payload)
            digest.update(payload)
            offset += len(payload)
    manifest = dict(
        schema_version=1,
        kind="repair-case-raw-archive/v1",
        test_only=True,
        repository_id=str(repository),
        record=record,
        indexes=indexes,
        objects=entries,
        raw_bytes=offset,
        container_sha256=digest.hexdigest(),
    )
    payload = encode(manifest)
    assert len(payload) <= MAX_INDEX_BYTES
    with (folder / "case.json").open("xb") as handle:
        handle.write(payload)
    return dict(
        path=str(folder),
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        raw_bytes=offset,
        objects=len(entries),
        container_sha256=digest.hexdigest(),
    )


class CaseArchive:
    def __init__(self, folder: Path):
        payload = (folder / "case.json").read_bytes()
        assert len(payload) <= MAX_INDEX_BYTES
        self.manifest = json.loads(payload)
        self.path = folder / "objects.bin"
        manifest = self.manifest
        assert manifest["kind"] == "repair-case-raw-archive/v1" and manifest["test_only"]
        assert len(manifest["objects"]) <= MAX_OBJECTS
        assert all(
            len(rows) <= MAX_ROWS
            and all(row["repository_id"] == manifest["repository_id"] for row in rows)
            for rows in manifest["indexes"].values()
        )
        offset = 0
        for item in sorted(manifest["objects"].values(), key=lambda item: item["offset"]):
            assert item["offset"] == offset and item["byte_length"] >= 0
            ObjectType(item["object_type"])
            offset += item["byte_length"]
        assert offset == manifest["raw_bytes"] == self.path.stat().st_size <= MAX_RAW_BYTES
        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        assert digest.hexdigest() == manifest["container_sha256"]
        assert all(
            row["object_id"] in manifest["objects"] for row in manifest["indexes"]["object_refs"]
        )

    def get(self, ref, kind):
        item = self.manifest["objects"][ref]
        assert item["object_type"] == kind.value
        with self.path.open("rb") as handle:
            handle.seek(item["offset"])
            payload = handle.read(item["byte_length"])
        assert len(payload) == item["byte_length"] and object_digest(kind, payload) == ref
        return payload

    def verify_objects(self):
        for ref, item in self.manifest["objects"].items():
            self.get(ref, ObjectType(item["object_type"]))
