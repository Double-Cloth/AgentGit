"""独立审计完成的案例后汇总；部分结果显式列出缺例，禁止当完整成绩。"""

import argparse
import hashlib
import json
from pathlib import Path

from repair_benchmark_archive import CaseArchive
from repair_benchmark_release import OUTPUT, load_release
from repair_benchmark_report import method_row, patch_metrics, summarize
from repair_benchmark_verify import verify_case
from repair_fault_specifications import load_faults


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("train", "heldout", "all"), required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument(
        "--audit-all", action="store_true", help="重新审计所有原始响应，不复用已有收据"
    )
    parser.add_argument("--destination", type=Path, help="只创建新的汇总文件，不覆盖既有结果")
    args = parser.parse_args()
    release = load_release()
    specs = load_faults()
    selected = [case for case in specs.cases if args.split == "all" or case.split == args.split]
    missing = []
    rows = []
    audits = []
    case_refs = {}
    (OUTPUT / "verified").mkdir(exist_ok=True)
    for case in selected:
        folder = OUTPUT / "cases" / case.id
        if not (folder / "case.json").exists():
            missing.append(case.id)
            continue
        store = CaseArchive(folder)
        record = store.manifest["record"]
        assert (
            record["scope"] == "frozen-evaluation/v1"
            and record["runtime_release_ref"] == release.identity()
        )
        assert record["case_id"] == case.id and "driver_failure" not in record
        audit_path = OUTPUT / "verified" / (case.id + ".json")
        if audit_path.exists():
            checked = json.loads(audit_path.read_text(encoding="utf-8"))
            assert checked["release_ref"] == release.identity() and checked[
                "manifest_sha256"
            ] == sha(folder / "case.json")
            audit = checked["audit"]
            if args.audit_all:
                assert (
                    verify_case(folder, OUTPUT / "frozen/tests/integration/repair_fault_runtime.py")
                    == audit
                )
        else:
            audit = verify_case(folder, OUTPUT / "frozen/tests/integration/repair_fault_runtime.py")
            checked = dict(
                release_ref=release.identity(),
                manifest_sha256=sha(folder / "case.json"),
                audit=audit,
            )
            with audit_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(checked, handle, indent=2)
        audits.append(audit)
        case_refs[case.id] = dict(
            manifest_sha256=sha(folder / "case.json"),
            container_sha256=store.manifest["container_sha256"],
            audit_sha256=sha(audit_path),
        )
        for outcome in record["methods"].values():
            row = method_row(case, outcome)
            row.update(patch_metrics(store, outcome))
            rows.append(row)
    if args.require_complete and missing:
        raise ValueError("Missing cases: " + ",".join(missing))
    target_ids = {
        case.id for case in specs.cases if case.split == "train" and case.declared_repairable
    }
    target_rows = [
        row for row in rows if row["case_id"] in target_ids and row["method"] == "repair"
    ]
    value = dict(
        kind="repair-evaluation-analysis/v1",
        release_ref=release.identity(),
        split=args.split,
        complete=not missing,
        expected_cases=len(selected),
        completed_cases=len(audits),
        missing_case_ids=missing,
        rows=rows,
        groups=summarize(rows),
        case_refs=case_refs,
        train_target=dict(
            fixed_denominator=12,
            required=10,
            evaluated=len(target_rows),
            known_verified=sum(row["verified"] for row in target_rows),
            status="not_evaluated_in_this_split"
            if args.split == "heldout"
            else "pending"
            if len(target_rows) != 12 or missing
            else "met"
            if sum(row["verified"] for row in target_rows) >= 10
            else "not_met",
        ),
        all_physical={
            key: sum(audit["all_physical"][key] for audit in audits)
            for key in ("model_calls", "tool_calls", "tokens")
        },
        analysis_code_sha256={
            str(Path(name).resolve().relative_to(Path(__file__).resolve().parents[2])): sha(name)
            for name in (__file__, Path(__file__).with_name("repair_benchmark_report.py"))
        },
        limits=[
            "No provider tokens or real-model generalization; "
            "deterministic synthetic fixture only.",
            "Task-level intervals are descriptive; six trials do not become six independent tasks.",
            "Incomplete/missing cases are pending and cannot be dropped from the final dataset.",
        ],
    )
    destination = args.destination or OUTPUT / "analysis"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / (
        args.split + ("-complete" if not missing else f"-partial-{len(audits):03}") + ".json"
    )
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            dict(
                path=str(target),
                complete=not missing,
                completed_cases=len(audits),
                missing=len(missing),
                train_target=value["train_target"],
                all_physical=value["all_physical"],
            )
        )
    )


if __name__ == "__main__":
    main()
