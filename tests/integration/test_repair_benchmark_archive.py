"""整例二进制证据的边界与损坏拒绝；不把容器摘要当作对象内容证明。"""

import hashlib
import json

import pytest
from repair_benchmark_archive import CaseArchive

from agentgit.core.canonical import ObjectType, encode, object_digest


def fixture_archive(tmp_path):
    payloads = [b"", "独立原始证据".encode()]
    payloads.sort(key=lambda value: object_digest(ObjectType.BLOB, value))
    entries, refs, offset = {}, [], 0
    repository = "11111111-1111-4111-8111-111111111111"
    for payload in payloads:
        identity = object_digest(ObjectType.BLOB, payload)
        entries[identity] = dict(offset=offset, byte_length=len(payload), object_type="blob")
        refs.append(dict(repository_id=repository, object_id=identity))
        offset += len(payload)
    raw = b"".join(payloads)
    (tmp_path / "objects.bin").write_bytes(raw)
    manifest = dict(
        schema_version=1,
        kind="repair-case-raw-archive/v1",
        test_only=True,
        repository_id=repository,
        objects=entries,
        raw_bytes=offset,
        container_sha256=hashlib.sha256(raw).hexdigest(),
        indexes={"object_refs": refs},
        record={},
    )
    (tmp_path / "case.json").write_bytes(encode(manifest))
    return manifest, payloads


def test_valid_archive_reads_typed_objects_including_empty_payload_and_rejects_wrong_type(tmp_path):
    manifest, payloads = fixture_archive(tmp_path)
    reader = CaseArchive(tmp_path)
    reader.verify_objects()
    for identity, payload in zip(manifest["objects"], payloads, strict=True):
        assert reader.get(identity, ObjectType.BLOB) == payload
        with pytest.raises(AssertionError):
            reader.get(identity, ObjectType.STATE)


@pytest.mark.parametrize("damage", ["container", "offset", "foreign_owner", "unowned_object"])
def test_archive_rejects_corrupt_container_or_index_boundaries(tmp_path, damage):
    manifest, _ = fixture_archive(tmp_path)
    if damage == "container":
        (tmp_path / "objects.bin").write_bytes(b"changed")
    elif damage == "offset":
        next(iter(manifest["objects"].values()))["offset"] = 1
    elif damage == "foreign_owner":
        manifest["indexes"]["object_refs"][0]["repository_id"] = "foreign"
    else:
        manifest["indexes"]["object_refs"][0]["object_id"] = "0" * 64
    (tmp_path / "case.json").write_bytes(encode(manifest))
    with pytest.raises(AssertionError):
        CaseArchive(tmp_path)


def test_forged_container_checksum_still_fails_domain_object_digest(tmp_path):
    manifest, _ = fixture_archive(tmp_path)
    raw = (tmp_path / "objects.bin").read_bytes()
    changed = bytes(value ^ 1 for value in raw)
    (tmp_path / "objects.bin").write_bytes(changed)
    manifest["container_sha256"] = hashlib.sha256(changed).hexdigest()
    (tmp_path / "case.json").write_text(json.dumps(manifest), encoding="utf-8")
    reader = CaseArchive(tmp_path)
    with pytest.raises(AssertionError):
        reader.verify_objects()
