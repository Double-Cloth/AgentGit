"""本地代码版本的可验证摘要。"""

import hashlib
from pathlib import Path

from agentgit.core.errors import AgentGitError, ErrorCode


def source_revision() -> str:
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(path for path in package.rglob("*") if path.suffix in {".py", ".json"}):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
    return "source-sha256:" + digest.hexdigest()


def dependency_lock_digest() -> str:
    lock = Path(__file__).resolve().parents[3] / "uv.lock"
    if not lock.is_file():
        raise AgentGitError(ErrorCode.CONFIGURATION, "冻结环境需要项目中的 uv.lock")
    return hashlib.sha256(lock.read_bytes()).hexdigest()
