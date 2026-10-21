"""S2-T01/T05：CAS 大小、原子发布、去重与不可变覆盖保护。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.paths import validate_artifact_paths
from agentgit.storage.objects import ObjectStore


def test_cas_matches_golden_and_deduplicates(tmp_path):
    store = ObjectStore(tmp_path)
    vectors = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/canonical/golden.json").read_text(
            encoding="utf-8"
        )
    )
    for vector in vectors:
        kind, payload = ObjectType(vector["object_type"]), bytes.fromhex(vector["canonical_hex"])
        obj = store.put(kind, payload)
        assert obj.id == vector["digest"]
        assert store.put(kind, payload) == obj
        assert store.get(obj.id, kind) == payload
    assert not list(tmp_path.rglob(".tmp-*"))


def test_concurrent_publication_never_overwrites(tmp_path):
    store = ObjectStore(tmp_path)
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: store.put(ObjectType.BLOB, b"same").id, range(16)))
    assert len(set(ids)) == 1
    assert store.get(ids[0], ObjectType.BLOB) == b"same"
    path = store.path_for(ids[0])
    path.write_bytes(b"bad")
    with pytest.raises(AgentGitError) as caught:
        store.put(ObjectType.BLOB, b"same")
    assert caught.value.code == ErrorCode.OBJECT_CORRUPT
    assert path.read_bytes() == b"bad"


def test_size_canonical_and_path_guards(tmp_path):
    store = ObjectStore(tmp_path, max_bytes=8)
    for kind, payload in ((ObjectType.BLOB, b"x" * 9), (ObjectType.STATE, b'{ "a":1}')):
        with pytest.raises(AgentGitError):
            store.put(kind, payload)
    with pytest.raises(AgentGitError):
        store.get("../outside", ObjectType.BLOB)
    obj = store.put(ObjectType.BLOB, b"okay")
    with pytest.raises(AgentGitError):
        store.get(obj.id, ObjectType.STATE)


@pytest.mark.parametrize(
    "path",
    [
        "../a",
        "/a",
        "C:/a",
        "\\\\server\\x",
        "a:b",
        "CON",
        "NUL.txt",
        "COM1.log",
        "LPT¹",
        "a/./b",
        "a//b",
        "a/../b",
        "a.",
        "a ",
        "a\x00b",
        "a\\b",
    ],
)
def test_artifact_paths_reject_cross_platform_attacks(path):
    with pytest.raises(AgentGitError):
        validate_artifact_paths([path])


@pytest.mark.parametrize("paths", [["A/x", "a/y"], ["a", "a/x"], ["a/x", "a"], ["a", "a"]])
def test_artifact_collisions_rejected(paths):
    with pytest.raises(AgentGitError):
        validate_artifact_paths(paths)


def test_valid_unicode_paths():
    validate_artifact_paths(["报告/对比.md", "report.md", "data/input.json"])
