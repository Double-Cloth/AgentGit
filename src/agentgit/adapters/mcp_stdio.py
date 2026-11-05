"""后端白名单 stdio：固定程序、有限帧/期限、自有单进程生命周期。"""

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgit.adapters.mcp_http import MCPTransportFailure
from agentgit.adapters.mcp_process import OwnedMCPProcess
from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import MCP_VERSION, MCPStdioServer
from agentgit.core.mcp_stdio import MCPApprovedProgram, MCPStdioRegistry
from agentgit.core.models import parse_contract


def _local_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or (os.name == "nt" and path.drive.startswith("\\\\")):
        raise AgentGitError(ErrorCode.FORBIDDEN, "MCP 程序仅允许固定的本地绝对路径")
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_file():
        raise AgentGitError(ErrorCode.FORBIDDEN, "MCP 程序路径不能经联接或链接重定向")
    return resolved


def approved_program(reference: str) -> MCPApprovedProgram:
    name = os.environ.get("AGENTGIT_MCP_STDIO_REGISTRY")
    if name is None:
        raise AgentGitError(ErrorCode.FORBIDDEN, "MCP stdio 程序尚未登记批准")
    try:
        with _local_file(name).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP stdio 白名单超过上限")
        registry = parse_contract(raw, MCPStdioRegistry)
        matches = [p for p in registry.programs if p.reference() == reference]
        if len(matches) != 1:
            raise AgentGitError(ErrorCode.FORBIDDEN, "MCP stdio 程序不在唯一批准白名单中")
        program = matches[0]
        runtime = Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
        if _local_file(program.interpreter) != runtime:
            raise AgentGitError(
                ErrorCode.FORBIDDEN, "MCP stdio 仅使用当前 Worker 的固定 Python 解释器"
            )
        for value, expected, limit in (
            (program.interpreter, program.interpreter_sha256, 268435456),
            (program.script, program.script_sha256, 1048576),
        ):
            path = _local_file(value)
            with path.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size > limit:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP 已批准程序超过大小上限")
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise AgentGitError(ErrorCode.FORBIDDEN, "MCP 已批准程序原字节已变化")
        return program
    except OSError:
        raise AgentGitError(ErrorCode.FORBIDDEN, "MCP 已批准程序不可访问") from None


@dataclass(frozen=True)
class MCPStdioReply:
    status: int
    payload: bytes


def post(
    server: MCPStdioServer,
    payload: bytes,
    *,
    deadline: float | None = None,
    guard: Callable[[], None] | None = None,
) -> MCPStdioReply:
    now = time.monotonic()
    until = (
        min(now + server.timeout_ms / 1000, deadline)
        if deadline is not None
        else now + server.timeout_ms / 1000
    )
    if len(payload) > 1048576:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "MCP stdio 请求超过上限")
    program = approved_program(server.program_ref)
    if time.monotonic() >= until:
        raise MCPTransportFailure(sent=False)
    original = decode(payload)
    if not isinstance(original, dict) or original.get("method") not in {"tools/list", "tools/call"}:
        raise AgentGitError(ErrorCode.FORBIDDEN, "MCP stdio 仅支持固定工具接口")
    discover = {
        "jsonrpc": "2.0",
        "id": "discover-" + str(original["id"]),
        "method": "server/discover",
        "params": {"_meta": original["params"]["_meta"]},
    }
    process: subprocess.Popen[bytes] | None = None
    owned: OwnedMCPProcess | None = None
    timer: threading.Timer | None = None
    sent = False
    # 不继承 API、模型、数据库、代理或 Python 注入环境。stdio 首批不支持凭据注入。
    environment = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
    try:
        with tempfile.TemporaryDirectory(prefix="agentgit-mcp-stdio-") as directory:
            environment.update(TEMP=directory, TMP=directory)
            try:
                if guard is not None:
                    guard()
                process = subprocess.Popen(
                    [program.interpreter, "-I", "-S", "-B", "-X", "utf8", program.script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=directory,
                    env=environment,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0,
                    start_new_session=os.name != "nt",
                )
                owned = OwnedMCPProcess(process)
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise MCPTransportFailure(sent=False)
                timer = threading.Timer(remaining, owned.kill)
                timer.daemon = True
                timer.start()
                assert process.stdin is not None and process.stdout is not None
                total = 0

                def exchange(message: dict[str, Any]) -> bytes:
                    nonlocal total
                    assert (
                        process is not None
                        and process.stdin is not None
                        and process.stdout is not None
                    )
                    if guard is not None:
                        guard()
                    process.stdin.write(encode(message) + b"\n")
                    process.stdin.flush()
                    line = process.stdout.readline(server.max_response_bytes + 2)
                    total += len(line)
                    if not line.endswith(b"\n") or total > server.max_response_bytes:
                        raise MCPTransportFailure(sent=sent)
                    return line[:-1]

                found = decode(exchange(discover))
                if (
                    not isinstance(found, dict)
                    or found.get("jsonrpc") != "2.0"
                    or found.get("id") != discover["id"]
                    or "error" in found
                    or not isinstance(found.get("result"), dict)
                ):
                    raise AgentGitError(
                        ErrorCode.UNSUPPORTED_SCHEMA, "MCP stdio 未声明受支持的现代协议"
                    )
                result = found["result"]
                if (
                    result.get("resultType") != "complete"
                    or not isinstance(result.get("supportedVersions"), list)
                    or MCP_VERSION not in result["supportedVersions"]
                    or not isinstance(result.get("capabilities"), dict)
                    or not isinstance(result["capabilities"].get("tools"), dict)
                    or type(result.get("ttlMs")) is not int
                    or result["ttlMs"] < 0
                    or result.get("cacheScope") not in {"public", "private"}
                ):
                    raise AgentGitError(
                        ErrorCode.UNSUPPORTED_SCHEMA, "MCP stdio 发现不在固定支持范围"
                    )
                sent = True
                response = exchange(original)
                if time.monotonic() >= until:
                    raise MCPTransportFailure(sent=True)
                return MCPStdioReply(200, response)
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
                if process is not None:
                    if process.stdin is not None:
                        with suppress(OSError):
                            process.stdin.close()
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=0.2)
                    if owned is not None:
                        owned.close()
                    elif process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    if process.stdout is not None:
                        process.stdout.close()
    except (OSError, subprocess.SubprocessError):
        raise MCPTransportFailure(sent=sent) from None
