"""经 Docker 本地管道调用 Engine API，不依赖 Docker SDK 或 Shell。"""

import contextlib
import http.client
import json
import re
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any, cast

from agentgit.core.errors import AgentGitError, ErrorCode

_API = "/v1.44"
_MAX_JSON = 2 * 1024 * 1024
_MAX_INPUT = 34 * 1024 * 1024
_ID = re.compile(r"^[a-f0-9]{64}$")


class EngineError(AgentGitError):
    def __init__(self, status: int | None = None) -> None:
        super().__init__(ErrorCode.SANDBOX_UNAVAILABLE, "Docker 管理连接或响应无效")
        self.status = status


@dataclass(frozen=True)
class ExecOutput:
    exec_id: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class _PipeSocket:
    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream

    def makefile(self, *_: object) -> IO[bytes]:
        return self.stream


def exact(stream: IO[bytes], count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        block = stream.read(min(count - len(chunks), 65536))
        if not block:
            raise EngineError()
        chunks.extend(block)
    return bytes(chunks)


class DockerEngine:
    """只接受后端探测到的本机 Unix socket/named pipe；请求路径由后端构造。"""

    def __init__(self, endpoint: str) -> None:
        if not (
            re.fullmatch(r"npipe:////\./pipe/[A-Za-z0-9_.-]+", endpoint)
            or re.fullmatch(r"unix:///[A-Za-z0-9_./-]+", endpoint)
        ):
            raise EngineError()
        self.endpoint = endpoint

    @classmethod
    def local(cls) -> "DockerEngine":
        try:
            value = subprocess.run(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            ).stdout
            if len(value) > 1024:
                raise EngineError()
            return cls(value.decode("ascii").strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            raise EngineError() from None

    @contextlib.contextmanager
    def _response(
        self,
        method: str,
        path: str,
        body: bytes,
        timeout: float,
        *,
        upgrade: bool = False,
        stdin: bytes = b"",
    ) -> Iterator[tuple[http.client.HTTPResponse, IO[bytes]]]:
        if (
            method not in {"GET", "POST", "DELETE"}
            or not re.fullmatch(r"/[A-Za-z0-9/_?=&.%:-]+", path)
            or len(body) > _MAX_JSON
            or len(stdin) > _MAX_INPUT
            or not 0 < timeout <= 135
        ):
            raise EngineError()
        try:
            process = subprocess.Popen(
                ["docker", "--host", self.endpoint, "system", "dial-stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise EngineError() from None
        timer = threading.Timer(timeout, process.kill)
        timer.daemon = True
        timer.start()
        assert process.stdin is not None and process.stdout is not None
        output = process.stdout
        request = (
            f"{method} {_API}{path} HTTP/1.1\r\nHost: docker\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            + ("Connection: Upgrade\r\nUpgrade: tcp\r\n" if upgrade else "Connection: close\r\n")
            + "\r\n"
        ).encode("ascii")
        writer: threading.Thread | None = None
        try:
            process.stdin.write(request + body)
            process.stdin.flush()
            response = http.client.HTTPResponse(cast(Any, _PipeSocket(output)))
            response.begin()
            if upgrade and response.status == 101:
                # HTTP 握手完成后才发送原始 stdin；半关闭不能丢失对端 stdout。
                def write_input() -> None:
                    try:
                        assert process.stdin is not None
                        process.stdin.write(stdin)
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass

                writer = threading.Thread(target=write_input, daemon=True)
                writer.start()
            yield response, output
        except (OSError, ValueError, http.client.HTTPException):
            raise EngineError() from None
        finally:
            timer.cancel()
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.kill()
            process.wait(timeout=5)
            if writer is not None:
                writer.join(timeout=5)
            with contextlib.suppress(OSError):
                process.stdin.close()
            output.close()

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 10
    ) -> dict[str, Any]:
        body = b"" if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        with self._response(method, path, body, timeout) as (response, _):
            if response.status not in {200, 201, 204}:
                raise EngineError(response.status)
            raw = response.read(_MAX_JSON + 1)
            if len(raw) > _MAX_JSON:
                raise EngineError()
            if not raw:
                return {}
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise EngineError()
            return cast(dict[str, Any], value)

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        user: str,
        cwd: str = "/workspace",
        stdin: bytes = b"",
        timeout: float = 35,
        output_limit: int = 1024 * 1024,
    ) -> ExecOutput:
        if not _ID.fullmatch(container_id) or not 0 <= output_limit <= _MAX_INPUT:
            raise EngineError()
        created = self.request(
            "POST",
            f"/containers/{container_id}/exec",
            {
                "AttachStdin": True,
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": False,
                "User": user,
                "WorkingDir": cwd,
                "Env": ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp", "LANG=C.UTF-8"],
                "Cmd": command,
            },
        )
        exec_id = str(created.get("Id", ""))
        if not _ID.fullmatch(exec_id):
            raise EngineError()
        body = b'{"Detach":false,"Tty":false}'
        buffers = [bytearray(), bytearray()]
        truncated = [False, False]
        with self._response(
            "POST", f"/exec/{exec_id}/start", body, timeout, upgrade=True, stdin=stdin
        ) as (response, stream):
            if response.status != 101:
                raise EngineError(response.status)
            while header := stream.read(8):
                if len(header) < 8:
                    header += exact(stream, 8 - len(header))
                channel = header[0] - 1
                remaining = int.from_bytes(header[4:], "big")
                if channel not in (0, 1) or header[1:4] != b"\0\0\0" or remaining > _MAX_INPUT:
                    raise EngineError()
                while remaining:
                    block = exact(stream, min(remaining, 65536))
                    remaining -= len(block)
                    available = max(0, output_limit - len(buffers[channel]))
                    buffers[channel].extend(block[:available])
                    truncated[channel] |= len(block) > available
        state = self.request("GET", f"/exec/{exec_id}/json")
        exit_code = state.get("ExitCode")
        if state.get("Running") is not False or type(exit_code) is not int:
            exit_code = None
        return ExecOutput(
            exec_id, exit_code, bytes(buffers[0]), bytes(buffers[1]), truncated[0], truncated[1]
        )
