"""显式选择的真实容器套件：缺引擎或锁定镜像即失败，不跳过。"""

from uuid import uuid4

import pytest

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer


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
