"""固定回环端点的单次 HTTP；禁止代理、重定向和自动重试。"""

import http.client
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from agentgit.core.mcp import MCP_VERSION, MCPServer


class MCPTransportFailure(Exception):
    def __init__(self, *, sent: bool):
        super().__init__("MCP 未取得有界完整 JSON 响应")
        self.sent = sent


@dataclass(frozen=True)
class MCPHTTPReply:
    status: int
    payload: bytes


def post(
    server: MCPServer, method: str, name: str | None, payload: bytes, secret: str | None
) -> MCPHTTPReply:
    if method not in {"tools/list", "tools/call"} or len(payload) > 1_048_576:
        raise ValueError("MCP 请求不在声明范围")
    # host 是固定数字 IP，不使用系统代理或任意 DNS 名称。
    port = urlsplit(server.endpoint).port
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=server.timeout_ms / 1000)
    deadline = time.monotonic() + server.timeout_ms / 1000
    sent = False
    active_socket: socket.socket | None = None
    response: http.client.HTTPResponse | None = None

    def expire() -> None:
        # Connection: close 会令 HTTPConnection 提前清空 sock；响应体仍持有连接。
        if active_socket is not None:
            with suppress(OSError):
                active_socket.shutdown(socket.SHUT_RDWR)
            active_socket.close()

    timer = threading.Timer(server.timeout_ms / 1000, expire)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        if connection.sock is None or time.monotonic() >= deadline:
            raise OSError()
        active_socket = connection.sock
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "MCP-Protocol-Version": MCP_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        if secret is not None:
            headers["Authorization"] = "Bearer " + secret
        # 首次尝试写入后即视为可能已发送，不依据局部 send 失败重发。
        sent = True
        connection.request("POST", "/mcp", payload, headers)
        response = connection.getresponse()
        if (
            response.getheader("Content-Type", "").split(";")[0].strip().lower()
            != "application/json"
            or response.getheader("Content-Encoding", "identity").lower() != "identity"
        ):
            raise OSError()
        length = response.getheader("Content-Length")
        if length is not None and (
            not length.isascii()
            or not length.isdecimal()
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
            chunks.append(chunk)
            size += len(chunk)
            if size > server.max_response_bytes:
                raise OSError()
        if (length is not None and size != int(length)) or time.monotonic() >= deadline:
            raise OSError()
        return MCPHTTPReply(response.status, b"".join(chunks))
    except (OSError, ValueError, http.client.HTTPException):
        raise MCPTransportFailure(sent=sent) from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()
