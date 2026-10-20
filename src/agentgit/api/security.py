"""回环服务边界、显式 CORS、认证和有界原始 JSON 校验。"""

import hmac
import json
import logging
from threading import Lock
from time import monotonic_ns
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentgit.config import Settings
from agentgit.core.canonical import decode
from agentgit.core.errors import AgentGitError, ErrorCode

MAX_REQUEST_BYTES = 1_048_576
ALLOWED_HEADERS = {"authorization", "content-type", "idempotency-key", "x-repository-id"}


def error_response(
    code: ErrorCode, message: str, correlation_id: str, status: int | None = None
) -> JSONResponse:
    statuses = {
        ErrorCode.UNAUTHORIZED: 401,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.VALIDATION: 422,
        ErrorCode.VERSION_CONFLICT: 409,
        ErrorCode.REPLAY_MISMATCH: 409,
        ErrorCode.RECORDING_MISSING: 409,
        ErrorCode.APPROVAL_REQUIRED: 409,
        ErrorCode.OUTCOME_UNKNOWN: 409,
        ErrorCode.UNSUPPORTED_SCHEMA: 409,
        ErrorCode.OUTPUT_LIMIT: 413,
        ErrorCode.CONFIGURATION: 503,
        ErrorCode.DATABASE_UNAVAILABLE: 503,
        ErrorCode.SANDBOX_UNAVAILABLE: 503,
        ErrorCode.INTERNAL_ERROR: 500,
    }
    return JSONResponse(
        status_code=status or statuses.get(code, 422),
        content={
            "error": {"code": code.value, "message": message, "evidence_refs": []},
            "correlation_id": correlation_id,
        },
    )


class RequestStats:
    def __init__(self) -> None:
        self.lock = Lock()
        self.requests, self.errors = 0, 0

    def record(self, status: int) -> None:
        with self.lock:
            self.requests += 1
            self.errors += int(status >= 400)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {"requests_total": self.requests, "errors_total": self.errors}


class LocalBoundary:
    def __init__(self, app: ASGIApp, settings: Settings, stats: RequestStats):
        self.app, self.settings, self.stats = app, settings, stats

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation = str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        started = monotonic_ns()
        response_started = False

        async def wrapped_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                self.stats.record(message["status"])
                extras = [(b"x-correlation-id", correlation.encode())]
                if not any(
                    name.lower() == b"cache-control" for name, _ in message.get("headers", [])
                ):
                    extras.append((b"cache-control", b"no-store"))
                if origin in self.settings.api_origins:
                    extras.extend(
                        [
                            (b"access-control-allow-origin", origin.encode()),
                            (b"vary", b"Origin"),
                            (
                                b"access-control-expose-headers",
                                b"X-Correlation-ID, ETag, Content-Disposition",
                            ),
                        ]
                    )
                message["headers"] = [*message.get("headers", []), *extras]
                route = scope.get("route")
                logging.getLogger("agentgit.http").info(
                    json.dumps(
                        {
                            "event": "http.request",
                            "correlation_id": correlation,
                            "method": scope["method"],
                            "route": getattr(route, "path", "unmatched"),
                            "status": message["status"],
                            "elapsed_ms": (monotonic_ns() - started) // 1_000_000,
                        }
                    )
                )
            await send(message)

        try:
            for name in ("authorization", "host", "origin", "idempotency-key", "x-repository-id"):
                if len(headers.getlist(name)) > 1:
                    raise AgentGitError(ErrorCode.VALIDATION, "请求包含重复的安全头")
            probe = scope["path"] in ("/health", "/ready") and scope["method"] in ("GET", "HEAD")
            try:
                host = urlsplit("//" + headers.get("host", ""))
                _ = host.port
            except ValueError:
                raise AgentGitError(ErrorCode.FORBIDDEN, "Host 不可访问") from None
            if host.hostname not in ("127.0.0.1", "localhost", "::1") and not (
                probe and host.hostname == "testserver"
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "Host 不可访问")
            if (
                host.username is not None
                or host.password is not None
                or host.path
                or host.query
                or host.fragment
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "Host 格式无效")
            if origin is not None and origin not in self.settings.api_origins:
                raise AgentGitError(ErrorCode.FORBIDDEN, "Origin 不在允许列表中")
            if scope["method"] == "OPTIONS" and origin is not None:
                requested = {
                    value.strip().lower()
                    for value in headers.get("access-control-request-headers", "").split(",")
                    if value.strip()
                }
                if (
                    headers.get("access-control-request-method") not in ("GET", "POST")
                    or requested - ALLOWED_HEADERS
                ):
                    raise AgentGitError(ErrorCode.FORBIDDEN, "预检方法或请求头不被允许")
                await Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": "GET, POST",
                        "Access-Control-Allow-Headers": ", ".join(sorted(ALLOWED_HEADERS)),
                        "Access-Control-Max-Age": "600",
                    },
                )(scope, receive, wrapped_send)
                return
            if not probe:
                token = self.settings.api_token
                if token is None:
                    raise AgentGitError(ErrorCode.CONFIGURATION, "本地 API 身份令牌尚未配置")
                scheme, _, provided = headers.get("authorization", "").partition(" ")
                if (
                    scheme.lower() != "bearer"
                    or not provided.isascii()
                    or not hmac.compare_digest(provided, token.get_secret_value())
                ):
                    raise AgentGitError(ErrorCode.UNAUTHORIZED, "需要有效的本地身份令牌")
            if int(headers.get("content-length", "0")) > MAX_REQUEST_BYTES:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "请求体超过上限")
        except ValueError:
            await error_response(ErrorCode.VALIDATION, "请求头无效", correlation)(
                scope, receive, wrapped_send
            )
            return
        except AgentGitError as exc:
            await error_response(exc.code, exc.message, correlation)(scope, receive, wrapped_send)
            return
        chunks: list[bytes] = []
        total = 0

        try:
            if scope["method"] == "POST":
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        raise AgentGitError(ErrorCode.VALIDATION, "请求体传输未完成")
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > MAX_REQUEST_BYTES:
                        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "请求体超过上限")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                decode(b"".join(chunks))
        except AgentGitError as exc:
            await error_response(exc.code, exc.message, correlation)(scope, receive, wrapped_send)
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if chunks and not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        try:
            await self.app(scope, bounded_receive, wrapped_send)
        except Exception as exc:
            if response_started:
                raise
            logging.getLogger("agentgit.http").error(
                json.dumps(
                    {
                        "event": "http.failed",
                        "correlation_id": correlation,
                        "error_type": type(exc).__name__,
                    }
                )
            )
            await error_response(
                ErrorCode.INTERNAL_ERROR, "请求处理失败，请使用关联编号核查", correlation
            )(scope, receive, wrapped_send)
