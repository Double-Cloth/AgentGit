"""将完整版本化路径映射到有界引用索引；不改变路径本身。"""

import hashlib
from typing import Literal


def path_reference_role(kind: Literal["artifact", "file"], path: str) -> str:
    """短角色保持兼容；长路径使用摘要，原文仍在状态和产物表中。"""
    role = f"{kind}:{path}"
    return role if len(role) <= 100 else f"{kind}-path:{hashlib.sha256(path.encode()).hexdigest()}"
