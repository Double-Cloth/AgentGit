"""真实回环 TLS 验证固定主机、拒绝错误证书、有界读取和累计期限。"""

import socket
import ssl
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentgit.adapters import model_http
from agentgit.adapters.model_http import FixedOpenAIHTTPS, TransportFailure

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CERT = FIXTURES / "model_https_public_test_cert.pem"
KEY = FIXTURES / "model_https_public_test_key.pem"
PUBLIC_KEY = "sk-agentgit-public-offline-fixture-only"
ORIGINAL_CONTEXT = ssl.create_default_context


@contextmanager
def server(monkeypatch, *, mode="ok", trust=True):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers, body))
            try:
                if mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", "https://invalid.example/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif mode == "slow_header":
                    for item in b"HTTP/1.1 200 OK\r\n":
                        self.wfile.write(bytes([item]))
                        self.wfile.flush()
                        time.sleep(0.02)
                else:
                    self.send_response(200)
                    if mode == "oversized":
                        self.send_header("Content-Length", "9999999")
                    elif mode == "truncated":
                        self.send_header("Content-Length", "50")
                    elif mode == "compressed":
                        self.send_header("Content-Encoding", "gzip")
                        self.send_header("Content-Length", "2")
                    elif mode == "slow_chunk":
                        self.send_header("Transfer-Encoding", "chunked")
                    else:
                        self.send_header("Content-Length", "2")
                    self.send_header("x-request-id", "req_local_tls")
                    self.end_headers()
                    if mode == "slow_chunk":
                        for _ in range(100):
                            self.wfile.write(b"1\r\nx\r\n")
                            self.wfile.flush()
                            time.sleep(0.02)
                    else:
                        self.wfile.write(b"{}")
                        self.wfile.flush()
                self.close_connection = True
            except (OSError, ValueError):
                self.close_connection = True

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT, KEY)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    address = httpd.server_address
    monkeypatch.setattr(
        model_http,
        "_addresses",
        lambda _: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)],
    )
    if trust:

        def client_context():
            result = ORIGINAL_CONTEXT()
            result.load_verify_locations(CERT)
            return result

        monkeypatch.setattr(model_http.ssl, "create_default_context", client_context)
    try:
        yield requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def post(*, duration=1, path="/v1/responses", max_bytes=2048):
    return FixedOpenAIHTTPS().post(
        path,
        b'{"model":"fixture"}',
        api_key=PUBLIC_KEY,
        client_request_id="a" * 32,
        deadline=time.monotonic() + duration,
        max_bytes=max_bytes,
    )


def test_real_tls_uses_fixed_host_auth_headers_and_complete_body(monkeypatch):
    with server(monkeypatch) as requests:
        reply = post()
    assert reply.status == 200 and reply.body == b"{}" and reply.request_id == "req_local_tls"
    assert len(requests) == 1 and requests[0][0] == "/v1/responses"
    headers = requests[0][1]
    assert headers["Host"] == "api.openai.com"
    assert headers["Authorization"] == "Bearer " + PUBLIC_KEY
    assert headers["X-Client-Request-Id"] == "a" * 32
    assert headers["Accept-Encoding"] == "identity"


def test_untrusted_certificate_fails_before_http_send(monkeypatch):
    with server(monkeypatch, trust=False) as requests, pytest.raises(TransportFailure) as error:
        post()
    assert error.value.sent is False and not requests
    assert PUBLIC_KEY not in str(error.value)


def test_redirect_is_returned_without_following(monkeypatch):
    with server(monkeypatch, mode="redirect") as requests:
        reply = post()
    assert reply.status == 307 and len(requests) == 1


@pytest.mark.parametrize("mode", ["oversized", "truncated", "compressed"])
def test_oversized_truncated_and_compressed_responses_are_unknown_after_send(monkeypatch, mode):
    with server(monkeypatch, mode=mode) as requests, pytest.raises(TransportFailure) as error:
        post()
    assert error.value.sent is True and len(requests) == 1


@pytest.mark.parametrize("mode", ["slow_header", "slow_chunk"])
def test_slow_peer_cannot_reset_cumulative_deadline(monkeypatch, mode):
    with server(monkeypatch, mode=mode) as requests:
        started = time.monotonic()
        with pytest.raises(TransportFailure) as error:
            post(duration=0.15)
        elapsed = time.monotonic() - started
    assert error.value.sent is True and len(requests) == 1
    assert elapsed < 1


@pytest.mark.parametrize("path", ["https://other.example/", "/v1/files", "/v1/responses?x=1"])
def test_transport_rejects_arbitrary_routes_without_resolving(monkeypatch, path):
    monkeypatch.setattr(model_http, "_addresses", lambda _: pytest.fail("不应解析主机"))
    with pytest.raises(ValueError):
        post(path=path)


def test_stuck_system_dns_does_not_block_call_deadline_or_spawn_more_resolvers(monkeypatch):
    pending = Future()
    monkeypatch.setattr(model_http, "_DNS_FUTURE", pending)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kw: pytest.fail("不得重复解析"))
    started = time.monotonic()
    with pytest.raises(TransportFailure) as error:
        post(duration=0.1)
    assert time.monotonic() - started < 1 and error.value.sent is False
    assert model_http._DNS_FUTURE is pending


def test_expired_deadline_is_reported_before_http_send(monkeypatch):
    monkeypatch.setattr(model_http, "_addresses", lambda _: pytest.fail("不应解析主机"))
    with pytest.raises(TransportFailure) as error:
        post(duration=-1)
    assert error.value.sent is False


def test_completed_dns_failure_does_not_poison_next_bounded_attempt(monkeypatch):
    failed = Future()
    failed.set_exception(OSError("公开测试失败"))
    monkeypatch.setattr(model_http, "_DNS_FUTURE", failed)
    calls = []

    def resolving(host, port, **_):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 1))]

    monkeypatch.setattr(socket, "getaddrinfo", resolving)
    assert model_http._addresses(time.monotonic() + 1)[0][-1] == ("127.0.0.1", 1)
    assert calls == [("api.openai.com", 443)]
