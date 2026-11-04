"""仅创建自有临时脚本、登记与日志，不提供通用启动入口。"""

import ctypes
import hashlib
import json
import os
import shutil
import sys
from ctypes import wintypes
from pathlib import Path

from agentgit.core.canonical import encode
from agentgit.core.mcp import MCPStdioServer
from agentgit.core.mcp_stdio import MCPApprovedProgram, MCPStdioRegistry


def prepare(directory, monkeypatch):
    directory.mkdir(exist_ok=True)
    script = directory / "trusted_server.py"
    shutil.copyfile(Path(__file__).with_name("mcp_stdio_server.py"), script)
    interpreter = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    program = MCPApprovedProgram(
        interpreter=str(interpreter),
        interpreter_sha256=hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        script=str(script.resolve()),
        script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        approved_by="fixture-operator",
    )
    registry = directory / "approved.json"
    registry.write_bytes(encode(MCPStdioRegistry(programs=(program,)).model_dump(mode="json")))
    monkeypatch.setenv("AGENTGIT_MCP_STDIO_REGISTRY", str(registry.resolve()))
    server = MCPStdioServer(
        server_identity="trusted-stdio", program_ref=program.reference(), timeout_ms=2000
    )
    return (
        server,
        program,
        script.with_suffix(".control"),
        lambda: (
            [
                json.loads(line)
                for line in script.with_suffix(".log").read_text("utf-8").splitlines()
            ]
            if script.with_suffix(".log").exists()
            else []
        ),
    )


def exited(pid):
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return True
        try:
            status = wintypes.DWORD()
            assert api.GetExitCodeProcess(handle, ctypes.byref(status))
            return status.value != 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    return False
