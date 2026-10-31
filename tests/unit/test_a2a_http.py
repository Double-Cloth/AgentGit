"""单次传输的发送前失败、总期限和连接后守卫；无需实际网络。"""

from email.message import Message
from unittest.mock import Mock

import pytest

from agentgit.adapters import a2a_http
from agentgit.adapters.a2a import A2AClient
from agentgit.core.a2a import A2AServer
from agentgit.core.errors import AgentGitError, ErrorCode


def config():
    return A2AServer(
        server_identity="fixture", principal_id="operator", endpoint="http://127.0.0.1:1234"
    )


def test_connect_failure_is_definitely_before_sending(monkeypatch):
    connection = Mock()
    connection.connect.side_effect = OSError("local failure")
    monkeypatch.setattr(a2a_http.http.client, "HTTPConnection", lambda *a, **kw: connection)
    with pytest.raises(a2a_http.A2ATransportFailure) as error:
        a2a_http.request(config(), "send", b"{}", "private-token")
    assert not error.value.sent
    connection.request.assert_not_called()
    connection.close.assert_called_once()
    assert "private-token" not in str(error.value)


def test_guard_is_rechecked_after_connect_before_headers(monkeypatch):
    connection = Mock()
    monkeypatch.setattr(a2a_http.http.client, "HTTPConnection", lambda *a, **kw: connection)
    calls = []

    def guard():
        calls.append(1)
        if len(calls) == 2:
            raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "测试停止")

    with pytest.raises(AgentGitError) as error:
        a2a_http.request(config(), "send", b"{}", None, guard=guard)
    assert error.value.code == ErrorCode.CONTROL_REQUESTED and len(calls) == 2
    connection.request.assert_not_called()


def test_timer_retains_body_socket_after_httpconnection_clears_its_socket(monkeypatch):
    active = Mock()
    connection = Mock(sock=active)
    response = Mock(status=200)
    response.headers = Message()
    response.headers["Content-Type"] = "application/a2a+json"
    response.getheader.side_effect = lambda name, default=None: default
    timer, callbacks = Mock(), []

    def getresponse():
        connection.sock = None
        return response

    def read1(_):
        callbacks[0]()
        raise OSError()

    def make_timer(_, callback):
        callbacks.append(callback)
        return timer

    connection.getresponse.side_effect = getresponse
    response.read1.side_effect = read1
    monkeypatch.setattr(a2a_http.http.client, "HTTPConnection", lambda *a, **kw: connection)
    monkeypatch.setattr(a2a_http.threading, "Timer", make_timer)
    with pytest.raises(a2a_http.A2ATransportFailure) as error:
        a2a_http.request(config(), "send", b"{}", None)
    assert error.value.sent
    active.shutdown.assert_called_once()
    active.close.assert_called_once()
    response.close.assert_called_once()
    timer.cancel.assert_called_once()


def test_guard_that_consumes_remaining_time_cannot_send_after_its_return(monkeypatch):
    clock, checks = [100.0], []
    connection = Mock()
    monkeypatch.setattr(a2a_http.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(a2a_http.http.client, "HTTPConnection", lambda *a, **kw: connection)

    def guard():
        checks.append(1)
        if len(checks) == 2:
            clock[0] = 101.0

    with pytest.raises(a2a_http.A2ATransportFailure) as error:
        a2a_http.request(config(), "send", b"{}", None, guard=guard, deadline=100.5)
    assert not error.value.sent
    connection.request.assert_not_called()
    connection.close.assert_called_once()


def test_restricting_remaining_time_never_extends_an_existing_deadline(monkeypatch):
    from agentgit.adapters import a2a

    clock = [100.0]
    monkeypatch.setattr(a2a.time, "monotonic", lambda: clock[0])
    client = A2AClient(config())
    client.restrict_deadline(10)
    clock[0] = 101.0
    client.restrict_deadline(2000)
    assert client._deadline == 100.01
    monkeypatch.setattr(
        a2a_http.http.client, "HTTPConnection", lambda *a, **kw: pytest.fail("过期后不能连接")
    )
    with pytest.raises(a2a_http.A2ATransportFailure) as error:
        a2a_http.request(config(), "send", b"{}", None, deadline=client._deadline)
    assert not error.value.sent


@pytest.mark.parametrize("name", ["invalid-name", "secret\r\nHeader", "", "lowercase"])
def test_invalid_credential_selector_fails_without_connection(name):
    with pytest.raises(AgentGitError) as error:
        A2AClient(config(), credential_env=name)
    assert error.value.code == ErrorCode.CONFIGURATION


@pytest.mark.parametrize("remaining", [True, 0, -1, 900001])
def test_deadline_rejects_invalid_remaining_budget(remaining):
    with pytest.raises(AgentGitError) as error:
        A2AClient(config()).restrict_deadline(remaining)
    assert error.value.code == ErrorCode.VALIDATION
