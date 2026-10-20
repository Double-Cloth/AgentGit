"""每个 Run 独立的受控文件工作区；不执行文件内容。"""

import os
import tempfile
from pathlib import Path

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.paths import validate_artifact_paths


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
        payload = text.encode("utf-8")
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
