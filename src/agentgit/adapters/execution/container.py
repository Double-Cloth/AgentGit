"""内部容器生命周期；调用身份必须由后续持久化执行服务提供。"""

import io
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentgit.adapters.execution.engine import DockerEngine, EngineError, ExecOutput
from agentgit.adapters.execution.protocol import MAX_WIRE, invalid, limit, read_files, read_frame
from agentgit.adapters.execution.supervisor import action_payload, read_receipt

_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_PYTHON = "/usr/local/bin/python"
_MODULE = "agentgit.adapters.execution.supervisor"
_ENV = ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp", "LANG=C.UTF-8"]


@dataclass(frozen=True)
class ExecutionIdentity:
    run_id: str
    call_id: str
    attempt: int
    fencing_token: int

    def __post_init__(self) -> None:
        UUID(self.run_id)
        UUID(self.call_id)
        if type(self.attempt) is not int or self.attempt < 0:
            raise invalid()
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise invalid()

    @property
    def name(self) -> str:
        return f"agentgit-sbx-{UUID(self.run_id).hex}-{UUID(self.call_id).hex}-{self.attempt}"

    @property
    def labels(self) -> dict[str, str]:
        return {
            "io.agentgit.managed": "execution-v1",
            "io.agentgit.run": self.run_id,
            "io.agentgit.call": self.call_id,
            "io.agentgit.attempt": str(self.attempt),
            "io.agentgit.fencing": str(self.fencing_token),
        }


class SandboxContainer:
    """仅管理已绑定身份的新容器，无业务 API 或用户可变 Docker 参数。"""

    def __init__(
        self, engine: DockerEngine, image_id: str, identity: ExecutionIdentity, timeout_ms: int
    ) -> None:
        if not _SHA256.fullmatch(image_id) or type(timeout_ms) is not int:
            raise invalid()
        if not 1 <= timeout_ms <= 120000:
            raise invalid()
        self.engine = engine
        self.image_id = image_id
        self.identity = identity
        self.timeout_ms = timeout_ms
        self.container_id: str | None = None
        self.creation_attempted = False
        self._loaded = False
        self._executed = False

    def preflight(self) -> None:
        info = self.engine.request("GET", "/info")
        if (
            info.get("OSType") != "linux"
            or info.get("CgroupVersion") != "2"
            or not all(
                info.get(key) is True
                for key in ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota", "CpuCfsPeriod")
            )
            or "name=seccomp,profile=builtin" not in info.get("SecurityOptions", [])
        ):
            raise EngineError()
        image = self.engine.request("GET", f"/images/{self.image_id}/json")
        if image.get("Id") != self.image_id or image.get("Os") != "linux":
            raise EngineError()

    def create(self) -> str:
        if self.container_id is not None or self.creation_attempted:
            raise invalid()
        self.preflight()
        self.creation_attempted = True
        created = self.engine.request(
            "POST",
            f"/containers/create?name={self.identity.name}",
            {
                "Image": self.image_id,
                "User": "10001:10003",
                "Env": _ENV,
                "Entrypoint": [_PYTHON, "-I", "-m", _MODULE],
                "Cmd": [str(self.timeout_ms)],
                "WorkingDir": "/workspace",
                "Labels": self.identity.labels,
                "Tty": False,
                "OpenStdin": False,
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "NetworkMode": "none",
                    "IpcMode": "private",
                    "CgroupnsMode": "private",
                    "NanoCpus": 1_000_000_000,
                    "Memory": 512 * 1024 * 1024,
                    "MemorySwap": 512 * 1024 * 1024,
                    "PidsLimit": 64,
                    "Tmpfs": {
                        "/workspace": (
                            "rw,exec,nosuid,nodev,size=134217728,uid=10002,gid=10003,mode=2770"
                        ),
                        "/tmp": "rw,nosuid,nodev,noexec,size=33554432,mode=1777",
                    },
                    "LogConfig": {"Type": "none", "Config": {}},
                    "RestartPolicy": {"Name": "no"},
                    "AutoRemove": False,
                },
            },
        )
        value = str(created.get("Id", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise EngineError()
        self.container_id = value
        return value

    def _id(self) -> str:
        if self.container_id is None:
            raise invalid()
        return self.container_id

    def inspect(self) -> dict[str, Any]:
        return self.engine.request("GET", f"/containers/{self._id()}/json")

    def start(self) -> None:
        self.engine.request("POST", f"/containers/{self._id()}/start")

    def control(self, action: str, files: dict[str, bytes] | None = None) -> bytes:
        result = self.engine.exec(
            self._id(),
            [_PYTHON, "-I", "-m", _MODULE, "bridge"],
            user="10001:10003",
            stdin=action_payload(action, files),
            timeout=self.timeout_ms / 1000 + 5,
            output_limit=MAX_WIRE,
        )
        if result.exit_code != 0 or result.stdout_truncated or result.stderr:
            raise EngineError()
        return result.stdout

    def ready(self) -> dict[str, Any]:
        result = read_receipt(io.BytesIO(self.control("ready")))
        if result.get("protocol") != 1 or result.get("ready") is not True:
            raise invalid()
        return result

    def load(self, files: dict[str, bytes]) -> None:
        if self._loaded or self._executed:
            raise invalid()
        result = read_receipt(io.BytesIO(self.control("load", files)))
        if result != {"protocol": 1, "loaded": len(files)}:
            raise invalid()
        self._loaded = True

    def execute(self, command: list[str], *, stdin: bytes = b"", cwd: str = ".") -> ExecOutput:
        if not self._loaded or self._executed:
            raise invalid()
        # 这里只接受后端已构造的解释器参数；工具请求不能提供 Docker 参数。
        from agentgit.core.paths import validate_artifact_paths

        if cwd != ".":
            validate_artifact_paths([cwd])
        self._executed = True
        return self.engine.exec(
            self._id(),
            ["/usr/bin/env", "-i", *_ENV, *command],
            user="10002:10003",
            cwd="/workspace" if cwd == "." else f"/workspace/{cwd}",
            stdin=stdin,
            timeout=self.timeout_ms / 1000 + 5,
        )

    def export(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        if not self._executed:
            raise invalid()
        stream = io.BytesIO(self.control("export"))
        header = read_frame(stream)
        if header == {"protocol": 1, "error": "output_limit"}:
            raise limit()
        stream.seek(0)
        files = read_files(stream)
        receipt = read_receipt(stream)
        if receipt.get("protocol") != 1 or receipt.get("complete") is not True:
            raise invalid()
        return files, receipt

    def stop(self) -> bool:
        """确认整个容器停止，暂留 exec 状态供输出收尾。"""
        try:
            # create 响应丢失时按已持久化的稳定名称核对，不能盲目新建。
            lookup = self.container_id or self.identity.name
            try:
                state = self.engine.request("GET", f"/containers/{lookup}/json")
            except EngineError as error:
                if error.status == 404:
                    self.container_id = None
                    return True
                return False
            if any(
                state.get("Config", {}).get("Labels", {}).get(key) != value
                for key, value in self.identity.labels.items()
            ):
                return False
            container_id = state["Id"]
            self.container_id = container_id
            if state["State"]["Running"]:
                self.engine.request("POST", f"/containers/{container_id}/kill")
                state = self.engine.request("GET", f"/containers/{container_id}/json")
            return state["State"]["Running"] is False
        except (EngineError, KeyError, TypeError):
            return False

    def cleanup(self) -> bool:
        """停止、删除并核对消失；连接失败时必须向调用方报告未确认。"""
        if not self.stop():
            return False
        if self.container_id is None:
            return True
        container_id = self.container_id
        try:
            self.engine.request("DELETE", f"/containers/{container_id}?v=true")
            try:
                self.engine.request("GET", f"/containers/{container_id}/json")
            except EngineError as error:
                return error.status == 404
        except (EngineError, KeyError, TypeError):
            return False
        return False
