"""显式准备/核验固定镜像；运行时不能调用它来下载或替换缺失镜像。"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/agentgit/core/paths.py",
    "src/agentgit/core/errors.py",
    "src/agentgit/core/canonical.py",
    "src/agentgit/adapters/execution/protocol.py",
    "src/agentgit/adapters/execution/supervisor.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="显式记录已构建且校验通过的新摘要")
    parser.add_argument("--image-id", help="仅 --record 使用的 sha256 内容地址；拒绝 tag")
    args = parser.parse_args()
    path = ROOT / "sandbox/image.lock.json"
    existing = json.loads(path.read_text(encoding="utf-8"))
    image_id = args.image_id if args.record else existing["image_id"]
    if bool(args.image_id) != args.record or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        parser.error("--record 必须同时提供 --image-id sha256:<64 位摘要>")
    source_sha256 = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("sandbox/Dockerfile", *SOURCES)
    }
    if not args.record and source_sha256 != existing["source_sha256"]:
        parser.error("镜像可信源码已变化，必须构建并审阅新的镜像版本")
    engine = DockerEngine.local()
    image = engine.request("GET", f"/images/{image_id}/json")
    if image["Architecture"] != "amd64":
        parser.error("当前版本仅冻结 linux/amd64")
    container = SandboxContainer(
        engine, image_id, ExecutionIdentity(str(uuid4()), str(uuid4()), 0, 1), 30000
    )
    # 这段固定检查程序只在准备中的无网络容器内运行，不读取宿主数据。
    program = (
        "import hashlib,json,pathlib,platform; "
        f"names={json.dumps(SOURCES)}; "
        "print(json.dumps({'python':platform.python_version(),"
        "'packages':pathlib.Path('/opt/agentgit/packages.lock').read_text(),"
        "'source_sha256':{name:hashlib.sha256(pathlib.Path("
        "'/usr/local/lib/python3.12/site-packages',name.removeprefix('src/'))"
        ".read_bytes()).hexdigest() for name in names}}))"
    )
    try:
        container.create()
        container.start()
        container.ready()
        container.load({})
        result = container.execute(["/usr/local/bin/python", "-I", "-c", program])
        if result.exit_code != 0 or result.stdout_truncated or result.stderr:
            raise RuntimeError("镜像内容检查程序失败")
        inspected = json.loads(result.stdout)
        if inspected["python"] != "3.12.14":
            raise RuntimeError("镜像 Python 版本不匹配")
        if inspected["source_sha256"] != {name: source_sha256[name] for name in SOURCES}:
            raise RuntimeError("镜像内可信源码与工作树不一致")
        packages = inspected["packages"].encode("utf-8")
        if packages != (ROOT / "sandbox/packages.lock").read_bytes():
            raise RuntimeError("镜像软件包发生漂移，须先单独审阅依赖清单")
    finally:
        if not container.cleanup():
            raise RuntimeError("镜像检查容器未确认清理，不能发布锁定摘要")
    digest = hashlib.sha256(packages).hexdigest()
    if args.record:
        lock = {
            "schema_version": 1,
            "platform": "linux/amd64",
            "image_id": image_id,
            "base_digest": (
                (ROOT / "sandbox/Dockerfile").read_text(encoding="utf-8").splitlines()[0][5:]
            ),
            "debian_snapshot": "20260918T000000Z",
            "python": inspected["python"],
            "packages_sha256": digest,
            "source_sha256": source_sha256,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
        temporary.replace(path)
    elif digest != existing["packages_sha256"]:
        raise RuntimeError("镜像依赖清单摘要不匹配")
    print(json.dumps({"verified": True, "image_id": image_id, "packages_sha256": digest}))


if __name__ == "__main__":
    main()
