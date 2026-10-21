"""Recorded 的有界内存文件状态，不创建宿主目录或解释器。"""

from agentgit.core.canonical import ObjectType, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.research import AgentState
from agentgit.core.snapshots import ArtifactEntry, StateSnapshot


class MemoryWorkspace:
    def __init__(self, *, max_file_bytes: int, max_files: int):
        self.max_file_bytes, self.max_files = max_file_bytes, max_files
        self.media_types: dict[str, str] = {}
        self._files: dict[str, bytes] = {}

    def write(self, path: str, text: str, media_type: str) -> None:
        self.write_bytes(path, text.encode("utf-8"), media_type)

    def write_bytes(self, path: str, payload: bytes, media_type: str) -> None:
        self.replace({**self._files, path: payload}, {**self.media_types, path: media_type})

    def contents(self) -> dict[str, bytes]:
        return dict(sorted(self._files.items()))

    def replace(self, files: dict[str, bytes], media_types: dict[str, str]) -> None:
        validate_artifact_paths(files)
        if (
            set(files) != set(media_types)
            or len(files) > self.max_files
            or any(len(payload) > self.max_file_bytes for payload in files.values())
            or sum(map(len, files.values())) > 64 * 1024 * 1024
        ):
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Recorded 工作区超过完整清单或大小限制")
        self._files, self.media_types = dict(files), dict(media_types)

    def snapshot(self, state: AgentState) -> StateSnapshot:
        return StateSnapshot(
            data=state.model_dump(mode="json"),
            artifacts=tuple(
                ArtifactEntry(
                    path=path,
                    object_ref=object_digest(ObjectType.BLOB, payload),
                    media_type=self.media_types[path],
                )
                for path, payload in sorted(self._files.items())
            ),
        )
