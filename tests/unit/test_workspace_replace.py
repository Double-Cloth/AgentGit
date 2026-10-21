"""工作区整体替换必须支持增删改，校验或物化失败时保留原状态。"""

from pathlib import Path

import pytest

from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.errors import AgentGitError, ErrorCode


@pytest.fixture
def workspace(tmp_path):
    result = ArtifactWorkspace(tmp_path / "workspace", max_file_bytes=1024, max_files=3)
    result.write("old.txt", "original", "text/plain")
    result.write("remove.txt", "remove", "text/plain")
    return result


def test_whole_manifest_replaces_added_changed_deleted_files(workspace):
    workspace.replace(
        {"old.txt": b"changed", "new/result.json": b"{}"},
        {"old.txt": "text/plain", "new/result.json": "application/json"},
    )
    assert workspace.contents() == {"old.txt": b"changed", "new/result.json": b"{}"}
    assert not (workspace.root / "remove.txt").exists()


def test_validation_failure_does_not_change_workspace(workspace):
    before = workspace.contents()
    with pytest.raises(AgentGitError) as caught:
        workspace.replace({"large": b"x" * 1025}, {"large": "text/plain"})
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT
    assert workspace.contents() == before


def test_failed_final_rename_restores_original_directory(workspace, monkeypatch):
    original = Path.rename
    before = workspace.contents()

    def fail(source, target):
        if source.name == "files":
            raise OSError("目录提升故障注入")
        return original(source, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(AgentGitError) as caught:
        workspace.replace({"new": b"next"}, {"new": "text/plain"})
    assert caught.value.code == ErrorCode.OBJECT_CORRUPT
    assert workspace.contents() == before
