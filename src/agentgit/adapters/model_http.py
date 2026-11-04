"""固定主机的有界 HTTPS；没有代理、重定向、凭据落盘或内部重试。"""

import http.client
import socket
import ssl
import threading
import time
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

_DNS_LOCK = threading.Lock()
_DNS_FUTURE: Future[list[Any]] | None = None


class TransportFailure(Exception):
    def __init__(self, *, sent: bool):
        # 不把底层异常、请求头或供应商正文放入异常文本。
        super().__init__("模型 HTTPS 调用未取得完整响应")
        self.sent = sent


@dataclass(frozen=True)
class HTTPReply:
    status: int
    body: bytes
    request_id: str | None = None
    retry_after: str | None = None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def _addresses(deadline: float) -> list[Any]:
    global _DNS_FUTURE
    with _DNS_LOCK:
        if _DNS_FUTURE is None or _DNS_FUTURE.done():
            future: Future[list[Any]] = Future()
            _DNS_FUTURE = future

            def resolve() -> None:
                try:
                    future.set_result(
                        socket.getaddrinfo("api.openai.com", 443, type=socket.SOCK_STREAM)
                    )
                except OSError:
                    future.set_exception(OSError("模型主机解析失败"))

            # 系统解析器不能取消；全进程最多留一个解析线程，调用方按期限退出。
            threading.Thread(target=resolve, daemon=True, name="agentgit-model-dns").start()
        selected = _DNS_FUTURE
    return selected.result(timeout=_remaining(deadline))


class _Connection(http.client.HTTPSConnection):
    def __init__(self, deadline: float):
        self.context = ssl.create_default_context()
        super().__init__("api.openai.com", 443, timeout=_remaining(deadline), context=self.context)
        self.deadline = deadline

    def connect(self) -> None:
        last_error: OSError | None = None
        for family, kind, protocol, _, address in _addresses(self.deadline):
            raw = socket.socket(family, kind, protocol)
            self.sock = raw
            try:
                raw.settimeout(_remaining(self.deadline))
                raw.connect(address)
                self.sock = self.context.wrap_socket(raw, server_hostname="api.openai.com")
                return
            except OSError as exc:
                raw.close()
                self.sock = None
                last_error = exc
        raise last_error or OSError("模型主机没有可用地址")


class FixedOpenAIHTTPS:
    def post(
        self,
        path: str,
        body: bytes,
        *,
        api_key: str,
        client_request_id: str,
        deadline: float,
        max_bytes: int,
    ) -> HTTPReply:
        if path not in {"/v1/responses/input_tokens", "/v1/responses"}:
            raise ValueError("模型传输路径不在白名单")
        if len(body) > 2_097_152 or not 128 <= max_bytes <= 8_388_608:
            raise ValueError("模型传输大小不在允许范围")
        try:
            connection = _Connection(deadline)
        except (OSError, TimeoutError):
            raise TransportFailure(sent=False) from None
        sent = False

        def expire() -> None:
            # shutdown 中断慢速分块和头部读取，避免每次 recv 重置 socket timeout。
            active = connection.sock
            if active is not None:
                with suppress(OSError):
                    active.shutdown(socket.SHUT_RDWR)
                active.close()

        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            if connection.sock is None:
                raise OSError("TLS 连接未建立")
            connection.sock.settimeout(_remaining(deadline))
            # 从首次尝试写 HTTP 开始就按可能已发送处理，不依据本地 send 失败重发。
            sent = True
            connection.request(
                "POST",
                path,
                body,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-Client-Request-Id": client_request_id,
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if not 100 <= response.status <= 599:
                raise OSError("模型响应状态码不合法")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise OSError("不接受压缩模型响应")
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isascii() or not length.isdecimal() or int(length) > max_bytes
            ):
                raise OSError("模型响应长度超限或不合法")
            chunks: list[bytes] = []
            size = 0
            while True:
                _remaining(deadline)
                chunk = response.read1(min(65_536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise OSError("模型响应超过字节上限")
            if length is not None and size != int(length):
                raise OSError("模型响应不完整")
            _remaining(deadline)
            return HTTPReply(
                response.status,
                b"".join(chunks),
                response.getheader("x-request-id"),
                response.getheader("retry-after"),
            )
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError):
            raise TransportFailure(sent=sent) from None
        finally:
            timer.cancel()
            connection.close()
