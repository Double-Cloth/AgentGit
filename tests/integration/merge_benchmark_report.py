"""从完整审计和实际 XML 生成固定分母统计，禁止补齐缺失例或重复累计。"""

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

METHODS = ("semantic", "artifact_only", "choose_ours")
METRICS = (
    "matches_expectation",
    "safe_rejection",
    "conflict_detected",
    "input_rejected",
    "false_accept",
    "clean_artifacts_correct",
    "clean_content_correct",
    "plan_constraint_unsafe_candidate",
    "effect_unsafe_candidate",
    "incomplete",
)


def summarize(root, audit_path):
    frozen = json.loads((root / "frozen/snapshot.json").read_text("utf-8"))
    asset = json.loads((root / "frozen/tests/fixtures/merge/v1/cases.json").read_text("utf-8"))
    release = json.loads(
        (root / "frozen/tests/fixtures/merge/v1/evaluation-release-v1.json").read_text("utf-8")
    )
    cases = {item["id"]: item for item in asset["cases"]}
    assert sorted(cases) == release["cases"] == [f"M{index:02d}" for index in range(1, 31)]
    assert release["release_digest"] == frozen["release_digest"]
    assert release["code_revision"] == frozen["source_revision"]
    for name, digest in frozen["files"].items():
        assert hashlib.sha256((root / "frozen" / name).read_bytes()).hexdigest() == digest, name
    xmls = [root / "results-odd.xml", root / "results-even.xml"]
    seen = set()
    for path in xmls:
        tree = ET.parse(path)
        assert not any(tree.findall(".//" + kind) for kind in ("error", "failure", "skipped"))
        rows = tree.findall(".//testcase")
        assert len(rows) == 15
        for row in rows:
            found = re.search(r"\[(M[0-9]{2})\]$", row.attrib["name"])
            assert found is not None and found[1] not in seen
            seen.add(found[1])
    assert seen == set(cases)
    audit = json.loads(audit_path.read_text("utf-8"))
    assert audit["stage"] == "formal" and audit["source_revision"] == frozen["source_revision"]
    assert len(audit["results"]) == 30
    assert sorted(row["case"] for row in audit["results"]) == sorted(cases)
    assert len(audit["negative_checks"]) == 4 and all(
        row["rejected"] for row in audit["negative_checks"]
    )
    files = [*xmls, audit_path, root / "frozen/snapshot.json"]
    for row in audit["results"]:
        final_path = root / "cases" / row["case"] / "final/evidence.json"
        assert hashlib.sha256(final_path.read_bytes()).hexdigest() == row["final_sha256"]
        packet = json.loads(final_path.read_text("utf-8"))
        assert {item["method"]: item["assessment"] for item in packet["details"]["methods"]} == row[
            "assessments"
        ]
        assert row["approved"] == len(packet["indexes"]["merge_approvals"])
        assert row["automated_review_tickets"] == len(packet["indexes"]["merge_review_sessions"])
        assert row["group"] == cases[row["case"]]["group"]
        files.extend(
            root / "cases" / row["case"] / phase / "evidence.json"
            for phase in ("source", "methods", "final")
        )
    methods = {}
    for method in METHODS:
        totals = {
            name: sum(row["assessments"][method][name] for row in audit["results"])
            for name in METRICS
        }
        assert totals["safe_rejection"] + totals["false_accept"] + totals["incomplete"] == 20
        assert (
            totals["matches_expectation"]
            == totals["safe_rejection"] + totals["clean_content_correct"]
        )
        methods[method] = dict(
            totals=totals,
            expected_clean=10,
            expected_blocked=20,
            case_count=30,
            model_calls=0,
            tool_calls=0,
            cost_micro_usd=0,
            input_rejection_cases=[
                row["case"]
                for row in audit["results"]
                if row["assessments"][method]["input_rejected"]
            ],
            complete_content_cases=[
                row["case"]
                for row in audit["results"]
                if row["assessments"][method]["clean_content_correct"]
            ],
        )
    preparation = {
        name: sum(row["prepared"][name] for row in audit["results"])
        for name in ("models", "tools", "mock_utf8_bytes", "payment_unknown")
    }
    all_costs = {
        name: sum(row["total"][name] for row in audit["results"])
        for name in ("models", "tools", "mock_utf8_bytes")
    }
    post_execution = {name: all_costs[name] - preparation[name] for name in all_costs}
    assert all(value >= 0 for value in post_execution.values())
    primary_runs = sum(row["approved"] for row in audit["results"])
    assert primary_runs == sum(row["automated_review_tickets"] for row in audit["results"]) == 10
    return dict(
        kind="S11-merge-evaluation-statistics/v1",
        release_digest=frozen["release_digest"],
        source_revision=frozen["source_revision"],
        cases=30,
        groups=dict(clean=10, fact=10, plan=10),
        methods=methods,
        preparation=preparation,
        post_execution=post_execution,
        all_costs=all_costs,
        primary_completed_runs=primary_runs,
        baseline_completed_runs=None,
        automated_review_tickets=10,
        independent_human_reviews=None,
        external_payment_requests=0,
        negative_checks=audit["negative_checks"],
        oracle_review=release["oracle_review"],
        scope="engineering-not-S11-exit",
        sha256={path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.evidence, args.audit)
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            dict(
                cases=result["cases"],
                completed=result["primary_completed_runs"],
                all_costs=result["all_costs"],
            )
        )
    )


if __name__ == "__main__":
    main()
