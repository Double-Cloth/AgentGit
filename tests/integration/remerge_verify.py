"""在旧类型和新批准来源两条路径上重审实际重复合并与交叉历史。"""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from merge_run_verify import verify_run_pair

from agentgit.core.canonical import encode
from agentgit.core.merge import ApprovedMergeOrigin
from agentgit.core.merge_proposals import MergeProposalManifest
from agentgit.core.models import parse_contract


def negative_checks(source, final, revision, lock):
    """只改内存副本的索引绑定，保留原 CAS 字节以检验跨记录核对。"""
    import base64

    def manifest(row):
        return parse_contract(
            base64.b64decode(final["objects"][row["manifest_ref"]]["base64"], validate=True),
            MergeProposalManifest,
        )

    roots = [(row, manifest(row)) for row in final["indexes"]["merge_proposals"]]
    checks = []
    approved = next(
        (item.origins[0] for _, item in roots if isinstance(item.origins[0], ApprovedMergeOrigin)),
        None,
    )
    selected = next((item for _, item in roots if item.request.selected_base_run_id), None)
    if approved is not None:
        changed = copy.deepcopy(final)
        row = next(
            row
            for row in changed["indexes"]["merge_approvals"]
            if row["id"] == approved.approval_id
        )
        row["commit_id"] = "0" * 64
        checks.append(("approved_base_commit_binding", changed))
    if selected is not None:
        changed = copy.deepcopy(final)
        changed["indexes"]["run_commits"] = [
            row
            for row in changed["indexes"]["run_commits"]
            if not (
                row["run_id"] == selected.request.selected_base_run_id
                and row["commit_id"] == selected.request.selected_base
            )
        ]
        checks.append(("explicit_base_run_path", changed))
    changed = copy.deepcopy(final)
    call = next(
        row
        for row in changed["indexes"]["external_calls"]
        if row["provider"] == "tool:source_read/v1"
    )
    call["run_id"] = next(
        row["id"] for row in changed["indexes"]["runs"] if row["id"] != call["run_id"]
    )
    checks.append(("source_read_run_binding", changed))
    changed = copy.deepcopy(final)
    changed["indexes"]["merge_run_requests"][0]["initial_state_ref"] = "0" * 64
    checks.append(("new_run_initial_state_binding", changed))
    results = []
    for name, changed in checks:
        try:
            verify_run_pair(source, changed, revision, lock)
        except AssertionError:
            results.append(dict(check=name, rejected=True))
        else:
            raise AssertionError("篡改副本未被拒绝：" + name)
    assert encode(final) != encode(checks[-1][1])
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    results = []
    for path in sorted((args.evidence / "cases").glob("*/final/evidence.json")):
        source = json.loads((path.parent.parent / "source/evidence.json").read_text("utf-8"))
        final = json.loads(path.read_text("utf-8"))
        result = verify_run_pair(
            source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
        )
        result.update(
            case=path.parent.parent.name,
            evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            negative_checks=negative_checks(
                source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
            ),
        )
        results.append(result)
    assert len(results) == 2
    summary = dict(
        kind="S11-remerge-raw-audit/v1",
        source_revision=frozen["source_revision"],
        results=results,
        scope="engineering-not-S11-exit",
    )
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
