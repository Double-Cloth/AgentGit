"""显式选择的真实容器套件：缺引擎或锁定镜像即失败，不跳过。"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def engine():
    return DockerEngine.local()


@pytest.fixture(scope="session")
def image_id(engine):
    lock = json.loads((ROOT / "sandbox/image.lock.json").read_text(encoding="utf-8"))
    for relative, digest in lock["source_sha256"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest, relative
    image = engine.request("GET", f"/images/{lock['image_id']}/json")
    assert image["Id"] == lock["image_id"]
    assert image["Os"] == "linux"
    assert image["Architecture"] == "amd64"
    return lock["image_id"]


@pytest.fixture
def container_factory(engine, image_id):
    containers = []

    def create(files=None, timeout_ms=30000):
        identity = ExecutionIdentity(str(uuid4()), str(uuid4()), 0, 1)
        container = SandboxContainer(engine, image_id, identity, timeout_ms)
        containers.append(container)
        container.create()
        container.start()
        container.ready()
        container.load(files or {})
        return container

    yield create
    for container in reversed(containers):
        assert container.cleanup(), "未能确认测试容器已删除"
