"""每个 Run 独立的受控文件工作区；不执行文件内容。"""

import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.paths import validate_artifact_paths


class Workspace(Protocol):
    media_types: dict[str, str]

    def write(self, path: str, text: str, media_type: str) -> None: ...

    def write_bytes(self, path: str, payload: bytes, media_type: str) -> None: ...

    def contents(self) -> dict[str, bytes]: ...

    def replace(self, files: dict[str, bytes], media_types: dict[str, str]) -> None: ...


class ArtifactWorkspace:
    def __init__(self, root: Path, *, max_file_bytes: int, max_files: int):
        root = root.absolute()
        if any(path.is_symlink() or path.is_junction() for path in (root, *root.parents)):
            raise AgentGitError(ErrorCode.FORBIDDEN, "工作区不能是链接或联接")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.media_types: dict[str, str] = {}

    def _path(self, relative: str) -> Path:
        validate_artifact_paths([relative])
        path = self.root / relative
        current = self.root
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise AgentGitError(ErrorCode.FORBIDDEN, "工作区路径包含链接或联接")
        if not path.resolve().is_relative_to(self.root):
            raise AgentGitError(ErrorCode.FORBIDDEN, "工作区路径越界")
        return path

    def write(self, path: str, text: str, media_type: str) -> None:
        self.write_bytes(path, text.encode("utf-8"), media_type)

    def write_bytes(self, path: str, payload: bytes, media_type: str) -> None:
        if len(payload) > self.max_file_bytes:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "产物超过单文件大小上限")
        names = set(self.media_types) | {path}
        validate_artifact_paths(names)
        if len(names) > self.max_files:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "产物文件数超过上限")
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._path(path)
        fd, temporary = tempfile.mkstemp(prefix=".agentgit-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.media_types[path] = media_type

    def contents(self) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for name in sorted(self.media_types):
            path = self._path(name)
            if not path.is_file() or path.stat().st_nlink != 1:
                raise AgentGitError(ErrorCode.FORBIDDEN, "产物必须是独立普通文件")
            with path.open("rb") as stream:
                content = stream.read(self.max_file_bytes + 1)
            if len(content) > self.max_file_bytes:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "产物读取超过上限")
            result[name] = content
        return result

    def replace(self, files: dict[str, bytes], media_types: dict[str, str]) -> None:
        """完整校验并分阶段物化，文件替换失败时恢复原目录。"""
        validate_artifact_paths(files)
        if set(files) != set(media_types) or len(files) > self.max_files:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "执行产物清单或文件数量超过工作区限制")
        if any(len(content) > self.max_file_bytes for content in files.values()):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "执行产物超过工作区单文件限制")
        self.contents()
        parent = self.root.parent.resolve()
        staging = Path(tempfile.mkdtemp(prefix=".agentgit-workspace-", dir=parent))
        backup = staging / "previous"
        promoted = False
        try:
            candidate = ArtifactWorkspace(
                staging / "files", max_file_bytes=self.max_file_bytes, max_files=self.max_files
            )
            for name, content in sorted(files.items()):
                candidate.write_bytes(name, content, media_types[name])
            # 移动与递归清理前核对绝对路径，所有目标均属于当前 Run 的工作区父目录。
            if (
                self.root.is_symlink()
                or self.root.is_junction()
                or not self.root.resolve().is_relative_to(parent)
                or not staging.resolve().is_relative_to(parent)
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "工作区替换目标越界")
            self.root.rename(backup)
            try:
                candidate.root.rename(self.root)
            except OSError:
                backup.rename(self.root)
                raise
            self.media_types = dict(media_types)
            promoted = True
        except OSError:
            raise AgentGitError(
                ErrorCode.OBJECT_CORRUPT, "工作区完整物化失败，需从安全检查点恢复"
            ) from None
        finally:
            if not promoted and backup.exists():
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "工作区备份仍待恢复")
            if (
                staging.is_symlink()
                or staging.is_junction()
                or not staging.resolve().is_relative_to(parent)
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "工作区临时目录清理目标越界")
            # 成功物化后的旧副本仅为操作数据，不影响完整的新工作区。
            with suppress(OSError):
                shutil.rmtree(staging)
