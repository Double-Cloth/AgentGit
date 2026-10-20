"""独立测试服务：先 fsync 效果记录，再按场景回复或断连。"""

import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agentgit.core.canonical import ObjectType, encode, object_digest


def main():
    ready, journal = map(Path, sys.argv[1:])
    state = {"requests": 0, "hits": 0, "entries": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            payload = encode(data)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/state":
                self.reply(200, state)
            elif self.path.startswith("/effects/"):
                receipt = state["entries"].get(self.path.removeprefix("/effects/"))
                self.reply(200 if receipt else 404, receipt or {})
            else:
                self.reply(404, {})

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/effect" or not 0 < size <= 4096:
                self.reply(400, {})
                return
            payload = self.rfile.read(size)
            action = json.loads(payload)
            state["requests"] += 1
            receipt = {
                "schema_version": 1,
                "operation_id": action["operation_id"],
                "value": action["value"],
                "applied": True,
                "request_digest": object_digest(ObjectType.BLOB, payload),
            }
            old = state["entries"].get(action["operation_id"])
            if old is not None:
                if old != receipt:
                    self.reply(409, {})
                    return
                state["hits"] += 1
            state["entries"][action["operation_id"]] = receipt
            with journal.open("wb") as stream:
                stream.write(encode(state))
                stream.flush()
                os.fsync(stream.fileno())
            if action["response_mode"] == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
            else:
                self.reply(200, receipt)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    ready.write_text(json.dumps({"port": server.server_port}), encoding="utf-8")
    server.serve_forever()


if __name__ == "__main__":
    main()
