"""同卷发布、禁止覆盖和逐次校验的本地内容寻址存储。"""

import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgit.core.canonical import CODEC, ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode

FaultHook = Callable[[str], None]


@dataclass(frozen=True)
class StoredObject:
    id: str
    object_type: ObjectType
    media_type: str
    byte_length: int
    storage_key: str
    codec: str = CODEC


def _sync_directory(path: Path) -> None:
    # Windows 不提供普通目录 fd；文件内容已经 fsync，链接发布由 NTFS 原子完成。
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class ObjectStore:
    def __init__(
        self, root: Path, *, max_bytes: int = 8 * 1024 * 1024, fault: FaultHook | None = None
    ) -> None:
        if max_bytes < 1:
            raise AgentGitError(ErrorCode.CONFIGURATION, "对象大小上限必须为正数")
        if root.is_symlink() or root.is_junction():
            raise AgentGitError(ErrorCode.FORBIDDEN, "CAS 根目录不能是链接或联接")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.fault = fault or (lambda _: None)

    def path_for(self, object_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", object_id):
            raise AgentGitError(ErrorCode.VALIDATION, "对象 ID 必须为小写 SHA-256")
        parent = self.root / object_id[:2]
        if parent.is_symlink() or parent.is_junction():
            raise AgentGitError(ErrorCode.FORBIDDEN, "CAS 分片目录不能是链接或联接")
        return parent / object_id[2:]

    def put_json(self, object_type: ObjectType, value: Any) -> StoredObject:
        return self.put(object_type, encode(value), media_type="application/json")

    def put(
        self,
        object_type: ObjectType,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> StoredObject:
        if len(payload) > self.max_bytes:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "对象超过大小上限")
        if object_type != ObjectType.BLOB and encode(decode(payload)) != payload:
            raise AgentGitError(ErrorCode.VALIDATION, "结构对象必须使用规范 AGJSON 字节")
        object_id = object_digest(object_type, payload)
        target = self.path_for(object_id)
        target.parent.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self.fault("after_temp")
            try:
                # 硬链接是原子的 create-if-absent；os.replace 会覆盖已有对象，不能使用。
                os.link(temporary, target)
                _sync_directory(target.parent)
            except FileExistsError:
                if self.get(object_id, object_type) != payload:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "已存在对象与内容不符") from None
            self.fault("after_publish")
        finally:
            Path(temporary).unlink(missing_ok=True)
        return StoredObject(
            id=object_id,
            object_type=object_type,
            media_type=media_type,
            byte_length=len(payload),
            storage_key=f"{object_id[:2]}/{object_id[2:]}",
        )

    def get(self, object_id: str, object_type: ObjectType) -> bytes:
        path = self.path_for(object_id)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "CAS 对象不是普通文件")
            if info.st_size > self.max_bytes:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "CAS 对象超过读取上限")
            with path.open("rb") as stream:
                payload = stream.read(self.max_bytes + 1)
        except FileNotFoundError:
            raise AgentGitError(ErrorCode.NOT_FOUND, "CAS 对象缺失") from None
        if len(payload) > self.max_bytes or object_digest(object_type, payload) != object_id:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "CAS 对象摘要不符")
        return payload

    def get_json(self, object_id: str, object_type: ObjectType) -> Any:
        return decode(self.get(object_id, object_type))

    def inventory(self) -> Iterator[tuple[str, Path]]:
        for shard in self.root.iterdir():
            if shard.is_symlink() or shard.is_junction() or not shard.is_dir():
                continue
            if not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                continue
            for path in shard.iterdir():
                if path.name.startswith(".tmp-"):
                    yield "temporary", path
                elif re.fullmatch(r"[0-9a-f]{62}", path.name):
                    yield shard.name + path.name, path
