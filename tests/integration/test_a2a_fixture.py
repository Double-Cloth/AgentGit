"""纯 HTTP 观察独立测试设施；这些通过项不是产品适配验收。"""

import copy
import hashlib
import http.client
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentgit.services.environment import dependency_lock_digest, source_revision

pytestmark = pytest.mark.integration
ASSETS = Path(__file__).parents[1] / "fixtures/a2a/v1"
CASES = json.loads((ASSETS / "cases.json").read_text("utf-8"))


@pytest.fixture
def server(tmp_path, request, monkeypatch):
    ready, journal, control = (
        tmp_path / name for name in ("ready.json", "state.json", "mode.json")
    )
    control.write_text('{"mode":"success"}', encoding="utf-8")
    tokens = {role: secrets.token_hex(24) for role in ("owner", "other", "expired")}
    credential_envs = {}
    for role, token in tokens.items():
        principal = "operator" if role == "owner" else role
        name = "AGENTGIT_A2A_LOCAL_RESEARCH_" + principal.upper() + "_TOKEN"
        monkeypatch.setenv(name, token)
        credential_envs[role] = name
    environment = dict(os.environ)
    environment.update(
        {f"AGENTGIT_A2A_FIXTURE_{role.upper()}_TOKEN": token for role, token in tokens.items()}
    )
    script = Path(__file__).parents[1] / "helpers/a2a_server.py"
    interpreter = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    with (tmp_path / "process.log").open("wb") as output:
        process = subprocess.Popen(
            [str(interpreter), str(script), str(ready), str(journal), str(control)],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert process.poll() is None, "fixture 提前退出"
                assert time.monotonic() < deadline, "fixture 未就绪"
                time.sleep(0.01)
            observed = json.loads(ready.read_text("utf-8"))
            assert observed["pid"] == process.pid != os.getpid()
            observed["credential_envs"] = credential_envs

            def call(method, path, body=None, *, actor="owner", version="1.0"):
                headers = {"A2A-Version": version, "Content-Type": "application/a2a+json"}
                if actor is not None:
                    headers["Authorization"] = "Bearer " + tokens.get(actor, "invalid-token")
                connection = http.client.HTTPConnection("127.0.0.1", observed["port"], timeout=3)
                try:
                    connection.request(
                        method,
                        path,
                        json.dumps(body).encode() if body is not None else None,
                        headers,
                    )
                    response = connection.getresponse()
                    return response.status, json.loads(response.read()), dict(response.getheaders())
                finally:
                    connection.close()

            def mode(value):
                control.write_text(json.dumps({"mode": value}), encoding="utf-8")

            yield call, mode, lambda: json.loads(journal.read_text("utf-8")), observed
        finally:
            process.terminate()
            process.wait(timeout=10)
    if journal.exists():
        raw = journal.read_bytes()
        assert all(token.encode() not in raw for token in tokens.values())
        assert all(
            token.encode() not in (tmp_path / "process.log").read_bytes()
            for token in tokens.values()
        )
        evidence = os.environ.get("AGENTGIT_A2A_FIXTURE_PROOF_DIR")
        if evidence:
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24]
            payload = dict(
                test=request.node.nodeid,
                pid=process.pid,
                driver_pid=os.getpid(),
                process_exit=process.returncode,
                fixture_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                source=source_revision(),
                lock=dependency_lock_digest(),
                state=json.loads(raw),
            )
            with (directory / (name + ".json")).open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)


def send(call, body=None, **kwargs):
    return call("POST", "/message:send", body or copy.deepcopy(CASES["request"]), **kwargs)


def test_fixture_card_and_completed_task(server):
    call, _, state, observed = server
    status, card, _ = call("GET", "/.well-known/agent-card.json", actor=None)
    assert status == 200
    assert card["supportedInterfaces"] == [
        {
            "url": f"http://127.0.0.1:{observed['port']}",
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
        }
    ]
    status, result, _ = send(call)
    assert status == 200 and set(result) == {"task"}
    task = result["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"] == [CASES["artifact"]]
    assert task["history"][-1]["role"] == "ROLE_AGENT"
    assert call("GET", "/tasks/" + task["id"])[1] == task
    assert state()["accepted_messages"] == 1


@pytest.mark.parametrize("actor", [None, "wrong", "expired"])
def test_fixture_rejects_identity_before_accepting(server, actor):
    call, _, state, _ = server
    status, _, headers = send(call, actor=actor)
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert state()["tasks"] == {} and state()["accepted_messages"] == 0


def test_fixture_duplicate_and_changed_duplicate(server):
    call, _, state, _ = server
    first = send(call)
    assert send(call)[:2] == first[:2]
    changed = copy.deepcopy(CASES["request"])
    changed["message"]["parts"][0]["text"] = "changed public input"
    assert send(call, changed)[0] == 409
    assert state()["accepted_messages"] == 1 and len(state()["tasks"]) == 1


def test_fixture_task_queries_cancels_and_messages_are_identity_scoped(server):
    call, mode, state, _ = server
    mode("working")
    task = send(call)[1]["task"]
    assert call("GET", "/tasks/" + task["id"], actor="other")[0] == 404
    assert call("POST", "/tasks/" + task["id"] + ":cancel", {}, actor="other")[0] == 404
    follow = copy.deepcopy(CASES["request"])
    follow["message"].update(messageId="follow-up", taskId=task["id"], contextId=task["contextId"])
    assert send(call, follow, actor="other")[0] == 404
    other = send(call, actor="other")[1]["task"]
    assert other["id"] != task["id"] and len(state()["tasks"]) == 2
    assert state()["accepted_messages"] == 2


def test_fixture_accepts_before_disconnect_and_does_not_invent_retry(server):
    call, mode, state, _ = server
    mode("disconnect_after_accept")
    with pytest.raises(http.client.RemoteDisconnected):
        send(call)
    snapshot = state()
    assert snapshot["accepted_messages"] == 1 and len(snapshot["requests"]) == 1
    task_id = next(iter(snapshot["tasks"]))
    mode("working")
    assert call("GET", "/tasks/" + task_id)[1]["status"]["state"] == "TASK_STATE_WORKING"
    assert state()["accepted_messages"] == 1


def test_fixture_expired_task_and_idempotent_cancel(server):
    call, mode, state, _ = server
    mode("working")
    task_id = send(call)[1]["task"]["id"]
    mode("task_expired")
    status, error, _ = call("GET", "/tasks/" + task_id)
    assert status == 404 and error["error"]["details"][0]["reason"] == "TASK_NOT_FOUND"
    mode("working")
    first = call("POST", "/tasks/" + task_id + ":cancel", {})
    assert first[0] == 200 and first[1]["status"]["state"] == "TASK_STATE_CANCELED"
    assert call("POST", "/tasks/" + task_id + ":cancel", {})[:2] == first[:2]
    assert state()["accepted_messages"] == 1


def test_fixture_cancel_is_durable_before_disconnection(server):
    call, mode, state, _ = server
    mode("working")
    task_id = send(call)[1]["task"]["id"]
    mode("disconnect_after_cancel")
    with pytest.raises(http.client.RemoteDisconnected):
        call("POST", "/tasks/" + task_id + ":cancel", {})
    assert state()["tasks"][task_id]["value"]["status"]["state"] == "TASK_STATE_CANCELED"
    mode("working")
    assert call("GET", "/tasks/" + task_id)[1]["status"]["state"] == "TASK_STATE_CANCELED"


def test_fixture_cannot_cancel_a_completed_task(server):
    call, _, state, _ = server
    task_id = send(call)[1]["task"]["id"]
    status, error, _ = call("POST", "/tasks/" + task_id + ":cancel", {})
    assert status == 400 and error["error"]["details"][0]["reason"] == "TASK_NOT_CANCELABLE"
    follow = copy.deepcopy(CASES["request"])
    follow["message"].update(messageId="after-completion", taskId=task_id)
    status, error, _ = send(call, follow)
    assert status == 400 and error["error"]["details"][0]["reason"] == "UNSUPPORTED_OPERATION"
    assert state()["accepted_messages"] == 1
    assert state()["tasks"][task_id]["value"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_fixture_rejects_non_text_input_before_accepting(server):
    call, _, state, _ = server
    body = copy.deepcopy(CASES["request"])
    body["message"]["parts"] = [{"url": "http://127.0.0.1:1/private"}]
    assert send(call, body)[0] == 400
    assert state()["tasks"] == {} and state()["accepted_messages"] == 0


def test_fixture_followup_requires_same_task_context(server):
    call, mode, state, _ = server
    mode("input_required")
    task = send(call)[1]["task"]
    follow = copy.deepcopy(CASES["request"])
    follow["message"].update(messageId="follow-up", taskId=task["id"], contextId="wrong-context")
    assert send(call, follow)[0] == 400
    follow["message"]["contextId"] = task["contextId"]
    mode("success")
    completed = send(call, follow)[1]["task"]
    assert completed["id"] == task["id"] and completed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert state()["accepted_messages"] == 2 and len(state()["tasks"]) == 1


def test_fixture_protocol_and_redirect_injections_are_observable(server):
    call, mode, state, _ = server
    assert send(call, version="0.3")[0] == 400
    mode("wrong_protocol")
    assert (
        call("GET", "/.well-known/agent-card.json")[1]["supportedInterfaces"][0]["protocolVersion"]
        == "0.3"
    )
    mode("redirect")
    status, _, headers = call("GET", "/.well-known/agent-card.json")
    assert status == 307 and headers["Location"] == "http://127.0.0.1:1/message:send"
    assert state()["accepted_messages"] == 0 and len(state()["requests"]) == 3
