"""只读检查 Docker 前提；不会拉镜像或启动执行容器。"""

import json
import subprocess
from typing import Any

from agentgit.core.errors import ErrorCode

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
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True,
            timeout=10,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            report["message"] = "Docker 引擎不可用"
            return report
        info = json.loads(result.stdout)
        if info.get("OSType") != "linux":
            report["message"] = "必须切换到 Linux 容器模式"
            return report
        report.update(
            engine="linux",
            engine_version=info.get("ServerVersion"),
            memory_limit=bool(info.get("MemoryLimit")),
            pids_limit=bool(info.get("PidsLimit")),
            message="引擎可达；执行镜像、资源与隔离能力仍待 S4 实测验收",
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        report["message"] = "Docker 不存在、探测超时或响应无效"
    return report
