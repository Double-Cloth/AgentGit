"""独立标准库单文件 fixture；审计日志仅写到其自有测试目录。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

LOG = Path(__file__).with_suffix(".log")
CONTROL = Path(__file__).with_suffix(".control")
TOOL = {
    "name": "echo",
    "description": "可信本地测试工具；描述和只读提示不授予本地权限。",
    "inputSchema": {
        "type": "object",
        "properties": {"value": {"type": "string", "maxLength": 100}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"value": {"type": "string", "maxLength": 100}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "idempotentHint": True},
}


def record(event, **fields):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"event": event, "pid": os.getpid(), "parent_pid": os.getppid(), **fields},
                ensure_ascii=False,
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def main():
    record(
        "started",
        argv=sys.argv,
        isolated=sys.flags.isolated,
        no_site=sys.flags.no_site,
        dont_write_bytecode=sys.dont_write_bytecode,
        env_keys=sorted(os.environ),
        cwd=os.getcwd(),
    )
    for line in sys.stdin.buffer:
        request = json.loads(line)
        mode = CONTROL.read_text("utf-8") if CONTROL.is_file() else "normal"
        method, params = request["method"], request["params"]
        record("request", method=method, meta=params.get("_meta"))
        if mode == "stall_discovery":
            time.sleep(30)
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2025-11-25" if mode == "legacy" else "2026-07-28"],
                "capabilities": {"tools": {}},
                "ttlMs": 0,
                "cacheScope": "private",
            }
        elif method == "tools/list":
            if mode == "changed":
                TOOL["inputSchema"]["properties"]["value"]["maxLength"] = 99
            result = {"resultType": "complete", "tools": [TOOL]}
        elif method == "tools/call":
            record("effect", value=params["arguments"]["value"])
            if mode == "drop":
                os._exit(17)
            if mode == "timeout":
                time.sleep(30)
            if mode == "oversize":
                sys.stdout.write("x" * 2000000)
                sys.stdout.flush()
                continue
            if mode == "child":
                try:
                    child = subprocess.Popen(
                        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(30)"]
                    )
                    record("child_allowed", child_pid=child.pid)
                except OSError:
                    record("child_blocked")
            value = params["arguments"]["value"]
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": value}],
                "structuredContent": {"value": value},
                "isError": mode == "tool_error",
            }
        else:
            raise RuntimeError("fixture 不支持该方法")
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        if mode == "wrong_id" and method == "tools/call":
            reply["id"] = "wrong"
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    record("exited")


if __name__ == "__main__":
    main()
