"""镜像内可信 PID 1：独立截止时间、身份隔离及静止工作区采集。"""

import contextlib
import io
import os
import signal
import socket
import stat
import struct
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any, cast

from agentgit.adapters.execution.protocol import (
    MAX_FILE,
    MAX_FILES,
    MAX_TOTAL,
    invalid,
    limit,
    read_files,
    read_frame,
    validate_files,
    write_files,
    write_frame,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.paths import validate_artifact_paths

COLLECTOR_UID = 10001
EXECUTION_UID = 10002
GROUP_ID = 10003
CONTROL_ADDRESS = "\0agentgit-collector-v1"
WORKSPACE = Path("/workspace")
# 该模块只在固定 Linux 镜像内运行；宿主 Windows 的类型存根没有这些系统 API。
_linux_os = cast(Any, os)
_linux_socket = cast(Any, socket)


def execution_processes() -> list[int]:
    while True:
        try:
            if os.waitpid(-1, _linux_os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break
    found = []
    for item in Path("/proc").iterdir():
        if not item.name.isdecimal():
            continue
        try:
            for line in (item / "status").read_text().splitlines():
                if line.startswith("Uid:") and EXECUTION_UID in map(int, line.split()[1:]):
                    found.append(int(item.name))
        except FileNotFoundError:
            continue
    return found


def counters() -> dict[str, int]:
    result: dict[str, int] = {}
    for name, keys in (
        ("memory.events", {"oom_kill"}),
        ("pids.events", {"max"}),
        ("cpu.stat", {"usage_usec", "nr_throttled", "throttled_usec"}),
    ):
        for line in (Path("/sys/fs/cgroup") / name).read_text().splitlines():
            label, value = line.split()
            if label in keys:
                result[f"{name}:{label}"] = int(value)
    result["memory.peak"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    if len(result) != 6:
        raise invalid()
    return result


def materialize(files: dict[str, bytes]) -> None:
    if any(WORKSPACE.iterdir()):
        raise invalid()
    for relative, data in files.items():
        path = WORKSPACE / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o2770)
        with path.open("xb") as target:
            # 固定组读写执行权限让输入脚本可直接运行；用户不能修改采集身份。
            os.chmod(path, 0o770)
            target.write(data)


def _workspace_entries() -> Iterator[tuple[Path, os.stat_result]]:
    pending = [WORKSPACE]
    count = 0
    while pending:
        # scandir 逐项读取，不能先为攻击者的大目录创建无界列表。
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > 4000:
                    raise limit()
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                yield path, info
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # 读取会更新 atime；它不是内容变化的证据。
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def snapshot() -> dict[str, bytes]:
    if execution_processes():
        raise invalid()
    files: dict[str, bytes] = {}
    total = 0
    paths_seen: dict[str, str] = {}

    def check_path(path: Path) -> str:
        relative = path.relative_to(WORKSPACE).as_posix()
        validate_artifact_paths([relative])
        parts = relative.split("/")
        for index in range(len(parts)):
            prefix = "/".join(parts[: index + 1])
            key = prefix.casefold()
            if key in paths_seen and paths_seen[key] != prefix:
                raise invalid()
            paths_seen[key] = prefix
        return relative

    root_stat = WORKSPACE.stat()
    for path, before in _workspace_entries():
        relative = check_path(path)
        if before.st_dev != root_stat.st_dev:
            raise invalid()
        if stat.S_ISDIR(before.st_mode):
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise invalid()
        if before.st_size > MAX_FILE or len(files) >= MAX_FILES:
            raise limit()
        total += before.st_size
        if total > MAX_TOTAL:
            raise limit()
        fd = os.open(path, os.O_RDONLY | _linux_os.O_NOFOLLOW | _linux_os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if _identity(opened) != _identity(before):
                raise invalid()
            content = source.read(MAX_FILE + 1)
            after = os.fstat(source.fileno())
        if (
            _identity(before) != _identity(after)
            or _identity(path.lstat()) != _identity(after)
            or len(content) != before.st_size
            or len(content) > MAX_FILE
        ):
            raise invalid()
        files[relative] = content
    validate_files(files)
    if execution_processes():
        raise invalid()
    return files


def bridge() -> None:
    if _linux_os.getuid() != COLLECTOR_UID:
        raise invalid()
    with socket.socket(_linux_socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(CONTROL_ADDRESS)

        def forward_input() -> None:
            try:
                while block := sys.stdin.buffer.read(65536):
                    connection.sendall(block)
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        thread = threading.Thread(target=forward_input, daemon=True)
        thread.start()
        while block := connection.recv(65536):
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
        thread.join(timeout=1)


def serve(timeout_ms: int) -> None:
    if os.getpid() != 1 or _linux_os.getuid() != COLLECTOR_UID or not 1 <= timeout_ms <= 120000:
        raise invalid()
    os.umask(0o007)
    # PID 1 退出由内核结束同一 PID namespace，覆盖 setsid/后台后代；不依赖 Worker。
    timer = threading.Timer(timeout_ms / 1000, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    loaded = False
    exported = False
    with socket.socket(_linux_socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(CONTROL_ADDRESS)
        server.listen(4)
        while True:
            connection, _ = server.accept()
            with connection:
                _, uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, _linux_socket.SO_PEERCRED, 12)
                )
                if uid != COLLECTOR_UID:
                    continue
                reader = connection.makefile("rb")
                writer = connection.makefile("wb")
                try:
                    action = read_frame(reader)
                    if action == {"protocol": 1, "action": "ready"}:
                        write_frame(writer, {"protocol": 1, "ready": True, "counters": counters()})
                    elif action == {"protocol": 1, "action": "load"} and not loaded:
                        files = read_files(reader)
                        if reader.read(1) or execution_processes():
                            raise invalid()
                        materialize(files)
                        loaded = True
                        write_frame(writer, {"protocol": 1, "loaded": len(files)})
                    elif action == {"protocol": 1, "action": "export"} and loaded and not exported:
                        files = snapshot()
                        write_files(writer, files)
                        write_frame(
                            writer, {"protocol": 1, "complete": True, "counters": counters()}
                        )
                        exported = True
                    else:
                        raise invalid()
                    writer.flush()
                except Exception as error:
                    # 禁止把内部路径、用户内容或 Python 堆栈写进可信协议。
                    with contextlib.suppress(OSError):
                        code = (
                            "output_limit"
                            if isinstance(error, AgentGitError)
                            and error.code == ErrorCode.OUTPUT_LIMIT
                            else "export_invalid"
                        )
                        write_frame(writer, {"protocol": 1, "error": code})
                        writer.flush()
                finally:
                    reader.close()
                    writer.close()


def action_payload(action: str, files: dict[str, bytes] | None = None) -> bytes:
    stream = io.BytesIO()
    write_frame(stream, {"protocol": 1, "action": action})
    if files is not None:
        write_files(stream, files)
    return stream.getvalue()


def read_receipt(stream: IO[bytes]) -> dict[str, Any]:
    receipt = read_frame(stream)
    if stream.read(1):
        raise invalid()
    return receipt


def main() -> None:
    if sys.argv[1:] == ["bridge"]:
        bridge()
    elif len(sys.argv) == 2:
        serve(int(sys.argv[1]))
    else:
        raise invalid()


if __name__ == "__main__":
    main()
