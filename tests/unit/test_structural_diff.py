"""结构差异区分 JSON 类型、缺失/null、数组位置和二进制引用。"""

import pytest

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.snapshots import ArtifactEntry, StateSnapshot
from agentgit.diff.structural import json_changes, snapshot_changes


def test_pointer_escaping_null_and_boolean_are_not_lost():
    changes = json_changes(
        {"a/b": {"~key": True}, "removed": None},
        {"a/b": {"~key": 1}, "added": None},
    )
    assert [(item.path, item.operation) for item in changes] == [
        ("/a~1b/~0key", "replace"),
        ("/added", "add"),
        ("/removed", "remove"),
    ]
    assert changes[0].before is True and type(changes[0].after) is int


def test_array_reorder_is_compared_by_stable_positions():
    changes = json_changes(["a", "b"], ["b", "a", None])
    assert [(item.path, item.operation) for item in changes] == [
        ("/0", "replace"),
        ("/1", "replace"),
        ("/2", "add"),
    ]


def test_binary_artifacts_compare_hash_and_media_type_without_text_decode():
    left = StateSnapshot(
        data={},
        artifacts=(
            ArtifactEntry(
                path="binary/data", object_ref="a" * 64, media_type="application/octet-stream"
            ),
        ),
    )
    right = StateSnapshot(
        data={},
        artifacts=(
            ArtifactEntry(
                path="binary/data", object_ref="b" * 64, media_type="application/octet-stream"
            ),
        ),
    )
    changes = snapshot_changes(left, right)
    assert [(item.path, item.operation) for item in changes] == [
        ("/artifacts/binary~1data/object_ref", "replace")
    ]


def test_diff_does_not_silently_return_a_truncated_success():
    with pytest.raises(AgentGitError) as error:
        json_changes({}, {"a": 1, "b": 2}, maximum=1)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


@pytest.mark.parametrize("value", [1.1, float("nan"), {"bad": 2**53}])
def test_diff_obeys_canonical_json_contract(value):
    with pytest.raises(AgentGitError):
        json_changes({}, value)
