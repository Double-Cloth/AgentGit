"""有界消息／任务与确切回执引用；不伪造运行归属或实际网络。"""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.core.a2a import A2ABinding, A2AServer
from agentgit.core.a2a_wire import (
    STATES,
    A2ACall,
    A2AResult,
    binding_digest,
    parts,
    remote_id,
    task,
)
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError

ROOT = Path(__file__).parents[1] / "fixtures/a2a/v1"
BASE = {
    "id": "task-1",
    "contextId": "context-1",
    "status": {"state": "TASK_STATE_COMPLETED"},
    "artifacts": [
        {
            "artifactId": "summary",
            "parts": [{"data": {"verified": True}, "mediaType": "application/json"}],
        }
    ],
}


def bound(principal="operator"):
    return A2ABinding(
        server=A2AServer(
            server_identity="local", principal_id=principal, endpoint="http://127.0.0.1:1"
        ),
        card_json=encode(json.loads((ROOT / "agent-card.json").read_text("utf-8"))).decode(),
    )


def sending(**updates):
    value = copy.deepcopy(json.loads((ROOT / "cases.json").read_text("utf-8"))["request"])
    value["configuration"]["acceptedOutputModes"] = ["text/plain", "application/json"]
    value.update(updates)
    return value


@pytest.mark.parametrize(
    "identifier",
    ["../x", "a/b", "a?x=1", "a%2fb", "a:cancel", "a#fragment", "", "a" * 129, True, ["x"], "x\n"],
)
def test_remote_identifiers_cannot_modify_request_paths(identifier):
    with pytest.raises(ValueError):
        remote_id(identifier)


@pytest.mark.parametrize("state", sorted(STATES))
def test_all_declared_states_remain_distinct(state):
    value = copy.deepcopy(BASE)
    value["status"]["state"] = state
    task(value)


@pytest.mark.parametrize(
    "part",
    [
        {"url": "http://127.0.0.1:2/private"},
        {"raw": "YQ=="},
        {"text": "x", "data": {}},
        {"text": "x", "filename": "../../private"},
        {"text": ""},
        {"text": "x", "mediaType": "text/html"},
        {"data": {"access_token": "secret"}},
        {"data": 1.5},
        {"data": {}, "metadata": {}},
        {},
    ],
)
def test_unsupported_or_credential_parts_are_rejected(part):
    with pytest.raises((ValueError, AgentGitError)):
        parts([part])


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../private"},
        {"contextId": "x/other"},
        {"status": {"state": "TASK_STATE_UNSPECIFIED"}},
        {"status": {"state": 3}},
        {"status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-02-31T00:00:00Z"}},
        {"metadata": {"url": "http://127.0.0.1:2"}},
        {"artifacts": [BASE["artifacts"][0]] * 2},
        {"artifacts": [BASE["artifacts"][0]] * 9},
        {"history": [{"messageId": "agent", "role": "ROLE_AGENT", "parts": [{"text": "x"}]}]},
        {
            "history": [
                {
                    "messageId": "user",
                    "role": "ROLE_USER",
                    "taskId": "task-other",
                    "parts": [{"text": "x"}],
                }
            ]
        },
    ],
)
def test_task_bounds_and_nested_causal_identifiers_are_checked(changes):
    value = copy.deepcopy(BASE)
    value.update(changes)
    with pytest.raises((ValueError, AgentGitError)):
        task(value)


def test_reference_is_bound_to_exact_response_and_binding():
    binding = bound()
    result = A2AResult(
        operation="send",
        binding_ref=binding_digest(binding),
        request_ref="a" * 64,
        task_json=encode(BASE).decode(),
    )
    ref = result.reference()
    call = A2ACall(operation="get", body_json=encode({"id": ref.task_id}).decode(), task=ref)
    call.validate_binding(binding)
    with pytest.raises(ValueError):
        call.validate_binding(bound("other"))
    assert result.value() == BASE
    copied = result.value()
    copied["id"] = "changed"
    assert result.value()["id"] == "task-1"


@pytest.mark.parametrize(
    "change",
    ["missing_reference", "arbitrary_task", "deferred", "push", "agent_role", "structured_input"],
)
def test_call_cannot_smuggle_unregistered_tasks_or_channels(change):
    body = sending()
    if change == "missing_reference":
        with pytest.raises(ValidationError):
            A2ACall(operation="cancel", body_json='{"id":"task-1"}')
        return
    if change == "arbitrary_task":
        body["message"]["taskId"] = "task-1"
    elif change == "deferred":
        body["configuration"]["returnImmediately"] = False
    elif change == "push":
        body["configuration"]["taskPushNotificationConfig"] = {"url": "http://127.0.0.1:2"}
    elif change == "agent_role":
        body["message"]["role"] = "ROLE_AGENT"
    else:
        body["message"]["parts"] = [{"data": {"unregistered_input": True}}]
    with pytest.raises(ValidationError):
        A2ACall(operation="send", body_json=encode(body).decode())
