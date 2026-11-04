"""从首次运行前已记录的 Git 基线核对迁移，明确区别于 v1 运行时清单。"""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from repair_benchmark_release import ROOT, load_release

BASELINE = "bb04fa1d36f14b363272691d12e824c961c7f966"
FREEZE_SUMMARY_SHA256 = "b5a07d268d165b9b1bb642e43a4bf72969d4ed0e62e423344c0a18ab0f2057b4"


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def verify_files(root, expected):
    actual = {"alembic.ini"} | {
        path.relative_to(root).as_posix()
        for path in (root / "migrations").rglob("*")
        if path.suffix in (".py", ".mako")
    }
    if actual != set(expected):
        raise ValueError("迁移文件集合与运行前 Git 基线不同")
    for name, expected_digest in expected.items():
        path = root / name
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ValueError("迁移文件缺失或不是普通文件")
        if digest(path.read_bytes()) != expected_digest:
            raise ValueError("迁移字节与运行前 Git 基线不同：" + name)


def git(*arguments):
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, timeout=15, check=False
    )
    if result.returncode:
        raise ValueError("无法读取已记录的 Git 基线")
    return result.stdout


def attest():
    summary_path = ROOT / ".agentgit/evidence/S10-evaluation-freeze-summary-v1.json"
    payload = summary_path.read_bytes()
    assert digest(payload) == FREEZE_SUMMARY_SHA256
    summary = json.loads(payload)
    assert summary["git_base_commit"] == BASELINE
    release = load_release()
    assert summary["release_ref"] == release.identity()
    names = git("ls-tree", "-r", "--name-only", BASELINE, "--", "migrations", "alembic.ini")
    expected = {
        name: digest(git("show", BASELINE + ":" + name))
        for name in names.decode().splitlines()
        if name == "alembic.ini" or Path(name).suffix in (".py", ".mako")
    }
    assert len(expected) == 26
    verify_files(ROOT, expected)
    return dict(
        kind="migration-git-provenance-attestation/v1",
        observed_at=datetime.now(UTC).isoformat(),
        release_ref=release.identity(),
        original_freeze_summary_sha256=FREEZE_SUMMARY_SHA256,
        pre_run_git_baseline=BASELINE,
        baseline_tree=git("rev-parse", BASELINE + "^{tree}").decode().strip(),
        file_sha256=expected,
        current_bytes_match_baseline=True,
        original_release_runtime_guard_includes_migrations=False,
        original_pre_run_snapshot_includes_migrations=False,
        scope="post-hoc byte verification against the pre-run recorded Git baseline",
        execution_changes=False,
        verifier_sha256=digest(Path(__file__).read_bytes()),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = attest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(dict(path=str(args.output), sha256=digest(args.output.read_bytes()), files=26))
    )


if __name__ == "__main__":
    main()
