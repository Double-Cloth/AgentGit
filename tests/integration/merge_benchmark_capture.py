"""在隔离冻结版本内创建新的正式证据目录；保留旧包和既有目录。"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from merge_benchmark_release import RELEASE, ROOT, verify_release


def capture(destination):
    if destination.exists():
        raise ValueError("证据目录已经存在，请指定新目录")
    release = verify_release()
    files = dict(release["files"])
    files[RELEASE.relative_to(ROOT).as_posix()] = hashlib.sha256(RELEASE.read_bytes()).hexdigest()
    for name in files:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("冻结清单必须使用项目内相对路径")
    destination.mkdir(parents=True, exist_ok=False)
    frozen = destination / "frozen"
    for name, digest in files.items():
        target = frozen / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("复制的冻结文件摘要不符：" + name)
    verify_release()
    snapshot = dict(
        source_revision=release["code_revision"],
        release_digest=release["release_digest"],
        files=files,
    )
    with (frozen / "snapshot.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
    return snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    snapshot = capture(args.destination)
    print(json.dumps(dict(files=len(snapshot["files"]), release_digest=snapshot["release_digest"])))


if __name__ == "__main__":
    main()
