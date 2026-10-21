"""文件协议在启动容器之前拒绝非法路径、内容及资源要求。"""

import io

import pytest

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine, EngineError
from agentgit.adapters.execution.protocol import (
    MAX_FILE,
    pack_files,
    read_frame,
    unpack_files,
    write_frame,
)
from agentgit.core.errors import AgentGitError


def test_binary_files_roundtrip():
    files = {"数据/empty": b"", "binary": bytes(range(256)), "run.sh": b"#!/bin/sh\ntrue\n"}
    assert unpack_files(pack_files(files)) == files


@pytest.mark.parametrize("path", ["../escape", "/root", "C:ads", "CON", "a\\b", "a/../b"])
def test_invalid_path(path):
    with pytest.raises(AgentGitError):
        pack_files({path: b""})


@pytest.mark.parametrize(
    "files", [{"A": b"", "a": b""}, {"a": b"", "a/b": b""}, {"big": b"x" * (MAX_FILE + 1)}]
)
def test_conflicts_and_limits(files):
    with pytest.raises(AgentGitError):
        pack_files(files)


@pytest.mark.parametrize("mutation", ["truncated", "trailing", "changed", "unknown"])
def test_invalid_wire(mutation):
    raw = pack_files({"file": b"payload"})
    if mutation == "truncated":
        raw = raw[:-1]
    elif mutation == "trailing":
        raw += b"x"
    elif mutation == "changed":
        raw = raw[:-1] + b"x"
    else:
        stream = io.BytesIO()
        write_frame(stream, {"protocol": 2, "files": 0})
        raw = stream.getvalue()
    with pytest.raises(AgentGitError):
        unpack_files(raw)


def test_duplicate_json_and_oversized_frame():
    for raw in (b'\x00\x00\x00\x0d{"x":1,"x":2}', b"\x00\x01\x00\x00"):
        with pytest.raises(AgentGitError):
            read_frame(io.BytesIO(raw))


@pytest.mark.parametrize("endpoint", ["tcp://localhost:2375", "ssh://host", "npipe:////remote/x"])
def test_engine_rejects_remote_endpoint(endpoint):
    with pytest.raises(EngineError):
        DockerEngine(endpoint)


def test_container_rejects_tag_and_excessive_timeout():
    engine = DockerEngine("unix:///var/run/docker.sock")
    identity = ExecutionIdentity("0" * 32, "1" * 32, 0, 1)
    for image, timeout in (("agentgit:latest", 30000), ("sha256:" + "a" * 64, 120001)):
        with pytest.raises(AgentGitError):
            SandboxContainer(engine, image, identity, timeout)
