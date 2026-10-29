"""正式三方法评估的源码、资产和实际驱动冻结门禁。"""

import hashlib
import json
from pathlib import Path

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.services.environment import dependency_lock_digest, source_revision

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "tests/fixtures/merge/v1/evaluation-release-v1.json"
DRIVERS = {
    "tests/conftest.py",
    "tests/integration/merge_benchmark_release.py",
    "tests/integration/merge_benchmark_cases.py",
    "tests/integration/merge_benchmark_methods.py",
    "tests/integration/test_merge_benchmark_evaluation.py",
    "tests/integration/merge_proposal_cases.py",
    "tests/integration/test_merge_approval.py",
    "tests/integration/test_merge_proposals.py",
    "tests/fixtures/merge/v1/cases.json",
    "doc/S11-benchmark-plan.md",
    "uv.lock",
    "pyproject.toml",
    "alembic.ini",
    "DESIGN.md",
    "doc/AgentGit_设计文档_v1.0.0.md",
    "doc/AgentGit_实施方案_v1.0.0.md",
}


def release_digest(value):
    return object_digest(
        ObjectType.BLOB,
        encode({key: item for key, item in value.items() if key != "release_digest"}),
    )


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_release(path=RELEASE):
    value = json.loads(path.read_text("utf-8"))
    require(
        type(value.get("schema_version")) is int and value["schema_version"] == 1,
        "正式评估 Schema 不符",
    )
    require(value.get("kind") == "s11-merge-evaluation-release/v1", "正式评估协议不符")
    require(value.get("release_digest") == release_digest(value), "正式协议摘要不符")
    require(value.get("code_revision") == source_revision(), "产品源码已变化")
    require(value.get("dependency_lock_digest") == dependency_lock_digest(), "依赖锁已变化")
    require(value.get("methods") == ["semantic", "artifact_only", "choose_ours"], "方法已变化")
    require(value.get("cases") == [f"M{index:02d}" for index in range(1, 31)], "样例集合不完整")
    require(
        type(value.get("max_merge_model_calls")) is int and value["max_merge_model_calls"] == 0,
        "候选方法不得执行模型",
    )
    require(
        value.get("oracle_review") == "design-derived-awaiting-independent-review",
        "人工预期复核身份被替换",
    )
    required = DRIVERS | {
        path.relative_to(ROOT).as_posix()
        for folder in (ROOT / "src", ROOT / "migrations")
        for path in folder.rglob("*")
        if path.is_file() and path.suffix in (".py", ".json")
    }
    hashes = value.get("files", {})
    require(isinstance(hashes, dict), "冻结文件清单类型不符")
    require(required <= set(hashes), "冻结清单遗漏产品、迁移或实际驱动")
    for name, digest in hashes.items():
        actual = ROOT / name
        require(actual.resolve().is_relative_to(ROOT), "冻结路径越出项目")
        require(
            actual.is_file() and hashlib.sha256(actual.read_bytes()).hexdigest() == digest,
            "冻结文件已变化：" + name,
        )
    asset = ROOT / "tests/fixtures/merge/v1/cases.json"
    require(
        value.get("case_asset_sha256") == hashlib.sha256(asset.read_bytes()).hexdigest(),
        "样例资产摘要不符",
    )
    return value
