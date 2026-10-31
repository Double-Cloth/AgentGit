"""独立 A2A fixture；仅依赖标准库，不使用产品解析器或网关。"""

import copy
import hmac
import json
import os
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ASSETS = Path(__file__).parents[1] / "fixtures/a2a/v1"


def main():
    ready, journal, control = map(Path, sys.argv[1:])
    card = json.loads((ASSETS / "agent-card.json").read_text("utf-8"))
    cases = json.loads((ASSETS / "cases.json").read_text("utf-8"))
    credentials = {
        role: os.environ[f"AGENTGIT_A2A_FIXTURE_{role.upper()}_TOKEN"]
        for role in ("owner", "other", "expired")
    }
    state = {"tasks": {}, "messages": {}, "requests": [], "accepted_messages": 0}
    if journal.exists():
        state = json.loads(journal.read_text("utf-8"))

    def persist():
        temporary = journal.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(journal)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value, *, reason=None, disconnect=False):
            if reason:
                value = {"error": {"code": status, "message": "fixture request rejected"}}
                if reason in {
                    "TASK_NOT_FOUND",
                    "TASK_NOT_CANCELABLE",
                    "VERSION_NOT_SUPPORTED",
                    "UNSUPPORTED_OPERATION",
                }:
                    value["error"]["details"] = [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": reason,
                            "domain": "a2a-protocol.org",
                        }
                    ]
            state["requests"].append(
                {
                    "method": self.command,
                    "path": self.path,
                    "status": status,
                    "disconnected": disconnect,
                    "actor": self.actor,
                }
            )
            persist()
            if disconnect:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            payload = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/a2a+json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            if status == 307:
                self.send_header("Location", "http://127.0.0.1:1/message:send")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            self.actor = None
            self.connection.settimeout(3)
            mode = json.loads(control.read_text("utf-8"))["mode"]
            if self.headers.get("A2A-Version") != "1.0":
                self.reply(400, None, reason="VERSION_NOT_SUPPORTED")
                return
            if self.command == "GET" and self.path == "/.well-known/agent-card.json":
                value = copy.deepcopy(card)
                value["supportedInterfaces"][0]["url"] = f"http://127.0.0.1:{server.server_port}"
                if mode == "wrong_protocol":
                    value["supportedInterfaces"][0]["protocolVersion"] = "0.3"
                self.reply(307 if mode == "redirect" else 200, value)
                return
            for role, token in credentials.items():
                if hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.actor = role
            if self.actor in (None, "expired"):
                self.reply(401, None, reason="UNAUTHENTICATED")
                return
            if self.command == "POST":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65_536:
                        raise ValueError()
                    if self.headers.get("Content-Type") != "application/a2a+json":
                        raise ValueError()
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, OSError):
                    self.reply(400, None, reason="INVALID_ARGUMENT")
                    return
                if self.path == "/message:send":
                    self.send_message(body, mode)
                    return
            match = re.fullmatch(r"/tasks/(task-[1-9][0-9]*)(:cancel)?", self.path)
            if match is None or (self.command == "POST") != bool(match[2]):
                self.reply(404, None, reason="TASK_NOT_FOUND")
                return
            task = state["tasks"].get(match[1])
            if task is None or task["actor"] != self.actor or mode == "task_expired":
                self.reply(404, None, reason="TASK_NOT_FOUND")
                return
            value = task["value"]
            if self.command == "POST":
                if value["status"]["state"] in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}:
                    self.reply(400, None, reason="TASK_NOT_CANCELABLE")
                    return
                value["status"] = {"state": "TASK_STATE_CANCELED"}
            elif mode == "success" and value["status"]["state"] == "TASK_STATE_WORKING":
                value["status"] = {"state": "TASK_STATE_COMPLETED"}
                value["artifacts"] = [copy.deepcopy(cases["artifact"])]
            self.reply(200, value, disconnect=mode == "disconnect_after_cancel")

        def send_message(self, body, mode):
            message = body.get("message")
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("messageId"), str)
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", message["messageId"]) is None
                or message.get("role") != "ROLE_USER"
                or not isinstance(message.get("parts"), list)
                or not 1 <= len(message["parts"]) <= 8
                or any(
                    not isinstance(part, dict)
                    or set(part) - {"text", "mediaType"}
                    or not isinstance(part.get("text"), str)
                    or part.get("mediaType", "text/plain") != "text/plain"
                    for part in message["parts"]
                )
            ):
                self.reply(400, None, reason="INVALID_ARGUMENT")
                return
            key = self.actor + ":" + message["messageId"]
            old = state["messages"].get(key)
            if old is not None:
                if old["body"] != body:
                    self.reply(409, None, reason="ALREADY_EXISTS")
                    return
                task = state["tasks"][old["task_id"]]["value"]
            else:
                task_id = message.get("taskId")
                if task_id is not None:
                    if not isinstance(task_id, str):
                        self.reply(400, None, reason="INVALID_ARGUMENT")
                        return
                    existing = state["tasks"].get(task_id)
                    if existing is None or existing["actor"] != self.actor:
                        self.reply(404, None, reason="TASK_NOT_FOUND")
                        return
                    task = existing["value"]
                    if message.get("contextId", task["contextId"]) != task["contextId"]:
                        self.reply(400, None, reason="INVALID_ARGUMENT")
                        return
                    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
                        self.reply(400, None, reason="UNSUPPORTED_OPERATION")
                        return
                    task["history"].append(copy.deepcopy(message))
                else:
                    task_id = "task-" + str(len(state["tasks"]) + 1)
                    task = {
                        "id": task_id,
                        "contextId": "context-" + task_id,
                        "history": [copy.deepcopy(message)],
                    }
                    state["tasks"][task_id] = {"actor": self.actor, "value": task}
                target = {
                    "working": "TASK_STATE_WORKING",
                    "disconnect_after_accept": "TASK_STATE_WORKING",
                    "input_required": "TASK_STATE_INPUT_REQUIRED",
                }.get(mode, "TASK_STATE_COMPLETED")
                task["status"] = {"state": target}
                if target == "TASK_STATE_COMPLETED":
                    task["artifacts"] = [copy.deepcopy(cases["artifact"])]
                    task["history"].append(
                        {
                            "messageId": "reply-" + message["messageId"],
                            "taskId": task_id,
                            "contextId": task["contextId"],
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "公开输入已整理。"}],
                        }
                    )
                state["messages"][key] = {"body": copy.deepcopy(body), "task_id": task_id}
                state["accepted_messages"] += 1
            self.reply(200, {"task": task}, disconnect=mode == "disconnect_after_accept")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    temporary_ready = ready.with_suffix(".tmp")
    temporary_ready.write_text(
        json.dumps({"port": server.server_port, "pid": os.getpid()}), encoding="utf-8"
    )
    temporary_ready.replace(ready)
    server.serve_forever()


if __name__ == "__main__":
    main()
