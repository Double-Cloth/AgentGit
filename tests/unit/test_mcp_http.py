"""HTTPConnection 清空 sock 后，总期限仍须中断正在读取的响应体。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentgit.adapters import mcp_http
from agentgit.core.mcp import MCPServer


def test_deadline_keeps_socket_after_connection_close_response(monkeypatch):
    socket = Mock()
    timer = Mock()
    callbacks = []
    connection = Mock(sock=socket)
    response = Mock(status=200)
    response.getheader.side_effect = lambda name, default=None: {
        "Content-Type": "application/json",
        "Content-Encoding": "identity",
    }.get(name, default)

    def getresponse():
        # http.client 在 will_close 的响应中把 socket 交给 HTTPResponse。
        connection.sock = None
        return response

    def read1(_):
        callbacks[0]()
        raise OSError("读取被总期限中断")

    def make_timer(_, callback):
        callbacks.append(callback)
        return timer

    connection.getresponse.side_effect = getresponse
    response.read1.side_effect = read1
    monkeypatch.setattr(
        mcp_http,
        "http",
        SimpleNamespace(
            client=SimpleNamespace(
                HTTPConnection=lambda *a, **k: connection, HTTPException=RuntimeError
            )
        ),
    )
    monkeypatch.setattr(mcp_http.threading, "Timer", make_timer)
    with pytest.raises(mcp_http.MCPTransportFailure) as caught:
        mcp_http.post(
            MCPServer(server_identity="fixture", endpoint="http://127.0.0.1:1234/mcp"),
            "tools/list",
            None,
            b"{}",
            None,
        )
    assert caught.value.sent
    socket.shutdown.assert_called_once()
    socket.close.assert_called_once()
    response.close.assert_called_once()
    timer.cancel.assert_called_once()
