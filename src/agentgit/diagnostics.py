"""只读检查 Docker 前提；不会拉镜像或启动执行容器。"""

import subprocess
from typing import Any

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine
from agentgit.adapters.execution.policy import bundled_policy
from agentgit.core.errors import AgentGitError, ErrorCode

REQUIREMENTS = [
    "Docker Linux 容器引擎",
    "CPU、内存、PIDs 限制与默认 seccomp 实际生效（S4 验证）",
    "固定 digest 的预置执行镜像，包含 Python 3.12、/bin/sh、Git 和 rg（S4 冻结）",
    "断网、非 root、只读根、无宿主挂载、独立采集身份与截止时间守护（S4 验证）",
]


def sandbox_prerequisites() -> dict[str, Any]:
    report: dict[str, Any] = {
        "execution_ready": False,
        "requirements": REQUIREMENTS,
        "code": ErrorCode.SANDBOX_UNAVAILABLE.value,
    }
    try:
        policy = bundled_policy()
        engine = DockerEngine.local()
        probe = SandboxContainer(
            engine,
            policy.image_id,
            ExecutionIdentity("0" * 32, "1" * 32, 0, 1),
            policy.default_timeout_ms,
        )
        # 与实际执行共用前提校验；只允许 GET，不创建容器或执行命令。
        probe.preflight()
        info = engine.request("GET", "/info")
        report.update(
            execution_ready=True,
            code=None,
            engine="linux",
            engine_version=info.get("ServerVersion"),
            image_id=policy.image_id,
            execution_policy_ref=policy.digest(),
            memory_limit=True,
            pids_limit=True,
            message="本机执行引擎、隔离前提与固定镜像可用",
        )
    except AgentGitError as error:
        report["code"] = error.code.value
        report["message"] = "执行策略、固定镜像或本机 Docker 隔离前提不可用，请先核验准备环境"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        report["message"] = "Docker 不存在、探测超时或响应无效"
    return report
