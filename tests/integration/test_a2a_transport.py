"""产品客户端对独立 A2A 服务的实际请求；不代表完整 Run 验收。"""

import copy
import os
import time

import pytest
from test_a2a_fixture import CASES
from test_a2a_fixture import server as server

from agentgit.adapters.a2a import A2AClient, configured_client
from agentgit.core.a2a import A2AServer
from agentgit.core.a2a_wire import A2ACall, A2AResult
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract

pytestmark = pytest.mark.integration


def setup(server):
    _, mode, state, observed = server
    config = A2AServer(
        server_identity="local-research",
        principal_id="operator",
        endpoint=f"http://127.0.0.1:{observed['port']}",
    )
    client = configured_client(config)
    return client, client.discover(), mode, state, observed


def sending(body=None, reference=None):
    body = copy.deepcopy(body or CASES["request"])
    body["configuration"]["acceptedOutputModes"] = ["text/plain", "application/json"]
    if reference:
        body["message"].update(taskId=reference.task_id, contextId=reference.context_id)
    return A2ACall(operation="send", body_json=encode(body).decode(), task=reference)


def operation(name, result):
    reference = result.reference()
    return A2ACall(
        operation=name, body_json=encode({"id": reference.task_id}).decode(), task=reference
    )


def exchange(client, binding, call):
    return parse_contract(client.exchange(binding, call), A2AResult)


def test_product_success_then_exact_query(server, monkeypatch):
    # 即使宿主配置代理，也只能连接登记的数字回环地址。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    client, binding, _, state, _ = setup(server)
    result = exchange(client, binding, sending())
    assert result.value()["artifacts"] == [CASES["artifact"]]
    assert exchange(client, binding, operation("get", result)).value() == result.value()
    assert state()["accepted_messages"] == 1 and len(state()["requests"]) == 3


@pytest.mark.parametrize("mode_name", ["no_length", "chunked", "json_media", "structured"])
def test_supported_bounded_framing_and_structured_output(server, mode_name):
    client, binding, mode, state, _ = setup(server)
    mode(mode_name)
    result = exchange(client, binding, sending())
    if mode_name == "structured":
        assert result.value()["artifacts"][0]["parts"][0]["data"]["verified"] is True
        assert result.value()["history"][-1]["parts"][0]["data"] == {"public_input_only": True}
    assert state()["accepted_messages"] == 1


def test_explicit_duplicate_and_changed_duplicate(server):
    client, binding, _, state, _ = setup(server)
    first = exchange(client, binding, sending())
    assert exchange(client, binding, sending()).value() == first.value()
    body = copy.deepcopy(CASES["request"])
    body["message"]["parts"][0]["text"] = "changed"
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, sending(body))
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    assert state()["accepted_messages"] == 1 and len(state()["tasks"]) == 1


@pytest.mark.parametrize(
    "mode_name",
    [
        "disconnect_after_accept",
        "server_error_after_accept",
        "malformed_401",
        "redirect_write",
        "duplicate_json",
        "oversized",
        "truncated",
        "wrong_media",
        "compressed",
        "ambiguous_length",
        "mixed_framing",
        "echo_credential",
        "bad_state",
        "data_float",
        "external_artifact",
        "direct_message",
    ],
)
def test_unconfirmed_write_is_unknown_and_never_automatically_retried(server, mode_name):
    client, binding, mode, state, observed = setup(server)
    mode(mode_name)
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, sending())
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert os.environ[observed["credential_envs"]["owner"]] not in str(error.value)
    assert state()["accepted_messages"] == 1 and len(state()["requests"]) == 2


def test_task_reference_cannot_cross_local_principal_or_remote_identity(server):
    client, binding, mode, state, observed = setup(server)
    mode("working")
    result = exchange(client, binding, sending())
    other_server = binding.server.model_copy(update={"principal_id": "other"})
    other = configured_client(other_server)
    other_binding = other.discover()
    before = len(state()["requests"])
    with pytest.raises(AgentGitError) as crossed:
        other.exchange(other_binding, operation("get", result))
    assert crossed.value.code == ErrorCode.FORBIDDEN and len(state()["requests"]) == before
    wrongly_configured = A2AClient(
        binding.server, credential_env=observed["credential_envs"]["other"]
    )
    with pytest.raises(AgentGitError) as remote:
        wrongly_configured.exchange(binding, operation("get", result))
    assert remote.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("actor", [None, "expired"])
def test_missing_or_expired_credential_is_a_definite_rejection(server, actor):
    _, binding, _, state, observed = setup(server)
    client = A2AClient(
        binding.server, credential_env=observed["credential_envs"][actor] if actor else None
    )
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, sending())
    assert error.value.code == ErrorCode.UNAUTHORIZED and state()["accepted_messages"] == 0


def test_expired_task_cancel_and_duplicate_cancel_remain_explicit(server):
    client, binding, mode, state, _ = setup(server)
    mode("working")
    result = exchange(client, binding, sending())
    mode("task_expired")
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, operation("get", result))
    assert error.value.code == ErrorCode.NOT_FOUND
    mode("working")
    cancelled = exchange(client, binding, operation("cancel", result))
    assert cancelled.value()["status"]["state"] == "TASK_STATE_CANCELED"
    assert exchange(client, binding, operation("cancel", result)).value() == cancelled.value()
    assert state()["accepted_messages"] == 1


def test_cancel_disconnect_keeps_remote_effect_unknown(server):
    client, binding, mode, state, _ = setup(server)
    mode("working")
    result = exchange(client, binding, sending())
    mode("disconnect_after_cancel")
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, operation("cancel", result))
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert (
        state()["tasks"][result.reference().task_id]["value"]["status"]["state"]
        == "TASK_STATE_CANCELED"
    )
    assert len(state()["requests"]) == 3


def test_input_required_followup_preserves_task_and_context(server):
    client, binding, mode, state, _ = setup(server)
    mode("input_required")
    initial = exchange(client, binding, sending())
    assert initial.value()["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    body = copy.deepcopy(CASES["request"])
    body["message"]["messageId"] = "follow-up"
    mode("success")
    completed = exchange(client, binding, sending(body, initial.reference()))
    assert completed.reference().task_id == initial.reference().task_id
    assert state()["accepted_messages"] == 2 and len(state()["tasks"]) == 1


@pytest.mark.parametrize(
    "mode_name", ["wrong_task", "wrong_context", "bad_state", "duplicate_json"]
)
def test_read_mismatch_does_not_claim_a_write_unknown(server, mode_name):
    client, binding, mode, state, _ = setup(server)
    mode("working")
    result = exchange(client, binding, sending())
    mode(mode_name)
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, operation("get", result))
    assert error.value.code == ErrorCode.TOOL_ERROR and len(state()["requests"]) == 3


@pytest.mark.parametrize("mode_name", ["redirect", "wrong_protocol"])
def test_discovery_rejects_redirect_or_protocol_change_without_post(server, mode_name):
    client, _, mode, state, _ = setup(server)
    mode(mode_name)
    with pytest.raises(AgentGitError) as error:
        client.discover()
    assert error.value.code == ErrorCode.UNSUPPORTED_SCHEMA and state()["accepted_messages"] == 0


def test_deadline_cannot_be_extended_and_guard_precedes_http(server):
    client, binding, _, state, _ = setup(server)
    client.restrict_deadline(1)
    deadline = client._deadline
    client.restrict_deadline(2000)
    assert deadline is not None and client._deadline == deadline
    # Windows Python 3.12 的 monotonic 分辨率可能粗于 10ms；先证明截止点已过。
    observation_limit = time.perf_counter() + 1
    while time.monotonic() <= deadline:
        assert time.perf_counter() < observation_limit
        time.sleep(0.005)
    with pytest.raises(AgentGitError) as expired:
        client.exchange(binding, sending())
    assert expired.value.code == ErrorCode.TOOL_ERROR and len(state()["requests"]) == 1
    guarded = configured_client(binding.server)

    def stop():
        raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "测试暂停")

    guarded.guard_requests(stop)
    with pytest.raises(AgentGitError) as stopped:
        guarded.discover()
    assert stopped.value.code == ErrorCode.CONTROL_REQUESTED and len(state()["requests"]) == 1


def test_total_deadline_interrupts_a_partially_received_body(server):
    client, binding, mode, state, _ = setup(server)
    mode("slow_body")
    client.restrict_deadline(200)
    started = time.monotonic()
    with pytest.raises(AgentGitError) as error:
        client.exchange(binding, sending())
    assert error.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert time.monotonic() - started < 2
    assert state()["accepted_messages"] == 1
