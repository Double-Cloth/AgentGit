"""独立可信回环 MCP fixture；控制接口只供测试，不作为产品 Server。"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

TOOL = json.loads((Path(__file__).parents[1] / "fixtures/mcp/v1/tools.json").read_text("utf-8"))
STATE = {
    "mode": "normal",
    "requests": 0,
    "calls": 0,
    "writes": 0,
    "valid_headers": True,
    "authorized": False,
}
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, status=200, content_type="application/json"):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    def do_GET(self):
        with LOCK:
            self.reply(dict(STATE))

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/control":
            with LOCK:
                STATE["mode"] = request["mode"]
            self.reply({"ok": True})
            return
        method = request.get("method")
        params = request.get("params", {})
        with LOCK:
            STATE["requests"] += 1
            STATE["valid_headers"] &= (
                self.path == "/mcp"
                and self.headers.get("MCP-Protocol-Version") == "2026-07-28"
                and self.headers.get("Mcp-Method") == method
                and params.get("_meta", {}).get("io.modelcontextprotocol/protocolVersion")
                == "2026-07-28"
                and "io.modelcontextprotocol/clientCapabilities" in params.get("_meta", {})
                and (method != "tools/call" or self.headers.get("Mcp-Name") == params.get("name"))
            )
            STATE["authorized"] = self.headers.get("Authorization", "").startswith("Bearer ")
            mode = STATE["mode"]
        if method == "tools/list":
            definition = json.loads(json.dumps(TOOL))
            if mode == "changed":
                definition["inputSchema"]["properties"]["value"]["maxLength"] = 99
            result = {"resultType": "complete", "tools": [definition]}
        else:
            with LOCK:
                STATE["calls"] += 1
                STATE["writes"] += 1
            if mode in {"timeout", "slow"}:
                time.sleep(0.3)
            if mode == "drop":
                self.connection.close()
                return
            if mode == "redirect":
                self.send_response(307)
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/forbidden"
                )
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            value = params["arguments"]["value"]
            if mode == "credential_echo":
                value = self.headers.get("Authorization", "")
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": value}],
                "structuredContent": {"value": value},
                "isError": mode == "tool_error",
            }
            if mode == "bad_output":
                result["structuredContent"] = {"value": 42}
            if mode == "input_required":
                result["resultType"] = "input_required"
        body = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        if mode == "wrong_id" and method == "tools/call":
            body["id"] = "wrong"
        self.reply(
            body,
            content_type="text/event-stream"
            if mode == "sse" and method == "tools/call"
            else "application/json",
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    ready = Path(sys.argv[1])
    pending = ready.with_suffix(".pending")
    pending.write_text(
        json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}/mcp"}), encoding="utf-8"
    )
    # 就绪标记必须在完整 JSON 关闭后发布，读取方不能看到空文件或半写入内容。
    pending.replace(ready)
    server.serve_forever()
