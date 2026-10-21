"""宿主与独立采集身份共用的有界文件协议，不使用归档解包。"""

import hashlib
import io
import re
from typing import IO, Any, cast

from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.paths import validate_artifact_paths

MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 1000
MAX_WIRE = 34 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def invalid() -> AgentGitError:
    return AgentGitError(ErrorCode.VALIDATION, "执行文件协议不完整或不符合限制")


def limit() -> AgentGitError:
    return AgentGitError(ErrorCode.OUTPUT_LIMIT, "执行文件数量或大小超出固定限制")


def read_exact(stream: IO[bytes], count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        block = stream.read(min(65536, count - len(result)))
        if not block:
            raise invalid()
        result.extend(block)
    return bytes(result)


def write_frame(stream: IO[bytes], value: dict[str, Any]) -> None:
    payload = encode(value)
    if len(payload) > 4096:
        raise invalid()
    stream.write(len(payload).to_bytes(4, "big") + payload)


def read_frame(stream: IO[bytes]) -> dict[str, Any]:
    size = int.from_bytes(read_exact(stream, 4), "big")
    if not 0 < size <= 4096:
        raise invalid()
    value = decode(read_exact(stream, size))
    if not isinstance(value, dict):
        raise invalid()
    return cast(dict[str, Any], value)


def validate_files(files: dict[str, bytes]) -> None:
    validate_artifact_paths(files)
    if any(type(data) is not bytes for data in files.values()):
        raise invalid()
    if (
        len(files) > MAX_FILES
        or sum(map(len, files.values())) > MAX_TOTAL
        or any(len(data) > MAX_FILE for data in files.values())
    ):
        raise limit()


def write_files(stream: IO[bytes], files: dict[str, bytes]) -> None:
    validate_files(files)
    write_frame(stream, {"protocol": 1, "files": len(files)})
    for path, data in sorted(files.items()):
        write_frame(
            stream, {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
        stream.write(data)


def read_files(stream: IO[bytes]) -> dict[str, bytes]:
    header = read_frame(stream)
    if (
        set(header) != {"protocol", "files"}
        or type(header["protocol"]) is not int
        or header["protocol"] != 1
        or type(header["files"]) is not int
        or not 0 <= header["files"] <= MAX_FILES
    ):
        raise invalid()
    files: dict[str, bytes] = {}
    total = 0
    for _ in range(header["files"]):
        item = read_frame(stream)
        if (
            set(item) != {"path", "size", "sha256"}
            or type(item["path"]) is not str
            or type(item["size"]) is not int
            or not 0 <= item["size"] <= MAX_FILE
            or type(item["sha256"]) is not str
            or not _DIGEST.fullmatch(item["sha256"])
        ):
            raise invalid()
        path = item["path"]
        validate_artifact_paths([*files, path])
        total += item["size"]
        if total > MAX_TOTAL:
            raise invalid()
        payload = read_exact(stream, item["size"])
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise invalid()
        files[path] = payload
    return files


def pack_files(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    write_files(buffer, files)
    return buffer.getvalue()


def unpack_files(payload: bytes) -> dict[str, bytes]:
    if len(payload) > MAX_WIRE:
        raise invalid()
    stream = io.BytesIO(payload)
    files = read_files(stream)
    if stream.read(1):
        raise invalid()
    return files
