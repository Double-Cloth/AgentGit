"""已登记 A2A 回环端点的单次 HTTP，不跟随代理、重定向或自动重试。"""

import http.client
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from agentgit.core.a2a import A2A_VERSION, A2AOperation, A2AServer
from agentgit.core.a2a_wire import remote_id


class A2ATransportFailure(Exception):
    def __init__(self, *, sent: bool):
        super().__init__("A2A 未取得有界完整响应")
        self.sent = sent


@dataclass(frozen=True)
class A2AHTTPReply:
    status: int
    payload: bytes


def request(
    server: A2AServer,
    operation: A2AOperation,
    payload: bytes,
    secret: str | None,
    *,
    task_id: str | None = None,
    deadline: float | None = None,
    guard: Callable[[], None] | None = None,
) -> A2AHTTPReply:
    if len(payload) > 65_536:
        raise ValueError("A2A 请求超过字节上限")
    if operation == "discover" and task_id is None and not payload:
        method, path = "GET", "/.well-known/agent-card.json"
    elif operation == "send" and task_id is None and payload:
        method, path = "POST", "/message:send"
    elif operation in ("get", "cancel") and task_id is not None:
        path = "/tasks/" + remote_id(task_id)
        method = "GET" if operation == "get" else "POST"
        if operation == "cancel":
            path += ":cancel"
        if (operation == "get" and payload) or (operation == "cancel" and not payload):
            raise ValueError("A2A 任务请求体与操作不符")
    else:
        raise ValueError("A2A 操作超出声明范围")
    now = time.monotonic()
    deadline = (
        min(deadline, now + server.timeout_ms / 1000)
        if deadline is not None
        else now + server.timeout_ms / 1000
    )
    remaining = deadline - now
    if remaining <= 0:
        raise A2ATransportFailure(sent=False)
    if guard is not None:
        guard()
    connection = http.client.HTTPConnection(
        "127.0.0.1", urlsplit(server.endpoint).port, timeout=remaining
    )
    sent, active_socket, response = False, None, None

    def expire() -> None:
        if active_socket is not None:
            with suppress(OSError):
                active_socket.shutdown(socket.SHUT_RDWR)
            active_socket.close()

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        if connection.sock is None or time.monotonic() >= deadline:
            raise OSError()
        active_socket = connection.sock
        if guard is not None:
            guard()
        if time.monotonic() >= deadline:
            raise OSError()
        headers = {
            "Content-Type": "application/a2a+json",
            "Accept": "application/a2a+json, application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "A2A-Version": A2A_VERSION,
        }
        if secret is not None and operation != "discover":
            headers["Authorization"] = "Bearer " + secret
        sent = True
        connection.request(method, path, payload or None, headers)
        response = connection.getresponse()
        content_types = response.headers.get_all("Content-Type", [])
        lengths = response.headers.get_all("Content-Length", [])
        transfer = response.headers.get_all("Transfer-Encoding", [])
        if (
            len(content_types) != 1
            or content_types[0].split(";")[0].strip().lower()
            not in {"application/a2a+json", "application/json"}
            or response.getheader("Content-Encoding", "identity").lower() != "identity"
            or len(lengths) > 1
            or len(transfer) > 1
            or (lengths and transfer)
            or (transfer and transfer[0].lower() != "chunked")
        ):
            raise OSError()
        length = lengths[0] if lengths else None
        if length is not None and (
            not length.isascii()
            or not length.isdecimal()
            or len(length) > 10
            or int(length) > server.max_response_bytes
        ):
            raise OSError()
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            chunk = response.read1(min(65_536, server.max_response_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > server.max_response_bytes:
                raise OSError()
            chunks.append(chunk)
        if (length is not None and size != int(length)) or time.monotonic() >= deadline:
            raise OSError()
        return A2AHTTPReply(response.status, b"".join(chunks))
    except (OSError, ValueError, http.client.HTTPException):
        raise A2ATransportFailure(sent=sent) from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()
