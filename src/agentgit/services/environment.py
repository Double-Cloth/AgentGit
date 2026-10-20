"""本地代码版本的可验证摘要。"""

import hashlib
from pathlib import Path


def source_revision() -> str:
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
    return "source-sha256:" + digest.hexdigest()
