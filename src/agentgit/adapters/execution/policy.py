"""已审阅镜像目录；读取包内锁文件，不探测 Docker 或自动下载。"""

import hashlib
from importlib.resources import files

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import ExecutionPolicy, PreparedImage
from agentgit.core.models import parse_contract


def bundled_policy() -> ExecutionPolicy:
    try:
        raw = files("agentgit.adapters.execution").joinpath("image.lock.json").read_bytes()
        if len(raw) > 16384:
            raise ValueError
        lock = parse_contract(raw, PreparedImage)
        names = {
            "core/paths.py",
            "core/errors.py",
            "core/canonical.py",
            "adapters/execution/protocol.py",
            "adapters/execution/supervisor.py",
        }
        if set(lock.source_sha256) != {
            "sandbox/Dockerfile",
            *("src/agentgit/" + name for name in names),
        }:
            raise ValueError
        for name in names:
            content = files("agentgit").joinpath(name).read_bytes()
            if hashlib.sha256(content).hexdigest() != lock.source_sha256["src/agentgit/" + name]:
                raise ValueError
        return ExecutionPolicy(image_id=lock.image_id, packages_digest=lock.packages_sha256)
    except (OSError, KeyError, ValueError, AgentGitError):
        raise AgentGitError(
            ErrorCode.CONFIGURATION, "预置执行策略缺失或无效，须先准备并审阅固定镜像"
        ) from None
