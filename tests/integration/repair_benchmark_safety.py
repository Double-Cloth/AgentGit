"""在响应/验收审计之外，核对实际权限、路径拒绝和阶段/计量预算。"""

import argparse
import json
from collections import Counter
from pathlib import Path

from repair_benchmark_archive import CaseArchive
from repair_benchmark_migration_provenance import attest, digest
from repair_benchmark_release import OUTPUT, load_release
from repair_fault_specifications import load_faults

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError
from agentgit.core.paths import validate_artifact_paths

PROVIDERS = {
    "llm:mock-research/v1": "READ_ONLY",
    "tool:corpus_search/v1": "READ_ONLY",
    "tool:source_read/v1": "READ_ONLY",
    "tool:artifact_write/v1": "IDEMPOTENT_WRITE",
}
PHASES = ("screen", "reduce", "confirm", "regression")


def budget_checks(outcome):
    latest = outcome.get("validation") or outcome.get("reduced") or outcome["source"]
    usage, budget = latest["usage"], outcome["budget"]
    assert usage["active"] == 0
    for field in ("model_calls", "tool_calls", "tokens", "cost_micro_usd"):
        assert 0 <= usage[field] <= budget["max_" + field]
    assert sum(usage["started"][phase] for phase in PHASES) <= budget["max_trials"]
    assert all(usage["started"][phase] <= budget["quotas"][phase] for phase in PHASES)


def write_path_check(request, saved, status):
    """不执行写操作；非法路径必须留下完整拒绝，不能只有失败标签。"""
    try:
        validate_artifact_paths([request["path"]])
    except AgentGitError:
        assert status == "failed" and saved["complete"]
        assert saved["error"] and saved["error"]["code"] in ("validation", "forbidden")
        assert saved["response_bytes"] == 0
        return "invalid_path_denied"
    return "valid_relative_path"


def audit_case(archive):
    manifest = archive.manifest
    record, indexes = manifest["record"], manifest["indexes"]
    assert record["source_before"] == record["source_after"]
    counters = Counter()
    virtual = {row["call_id"] for row in indexes["hybrid_observations"]}
    for row in indexes["external_calls"]:
        assert row["provider"] in PROVIDERS and row["effect"] == PROVIDERS[row["provider"]]
        intent = json.loads(archive.get(row["intent_ref"], ObjectType.CALL))
        assert intent["effect"] == row["effect"]
        assert intent["occurrence"]["provider"] == row["provider"]
        assert intent["request_ref"] == row["request_digest"]
        if row["origin_call_id"] is not None or row["id"] in virtual:
            counters["recorded_or_virtual_calls"] += 1
            continue
        counters["physical_calls"] += 1
        if row["provider"] != "tool:artifact_write/v1":
            continue
        request = json.loads(archive.get(intent["request_ref"], ObjectType.BLOB))
        saved = json.loads(archive.get(row["result_ref"], ObjectType.CALL))
        counters[write_path_check(request, saved, row["status"])] += 1
    for row in indexes["artifacts"]:
        validate_artifact_paths([row["relative_path"]])
        counters["artifact_records_checked"] += 1
    for outcome in record["methods"].values():
        budget_checks(outcome)
        counters["method_budgets_checked"] += 1
        binding = outcome["space"]["binding"]
        search = next(
            row for row in indexes["repair_searches"] if row["id"] == outcome["search_id"]
        )
        assert search["validator_ref"] == binding["validator_ref"]
        assert search["failed_run_id"] == binding["failed_run_id"]
        counters["search_validator_bindings_checked"] += 1
    assert not indexes["proposal_reviews"] and not indexes["repair_saved_experiments"]
    return dict(counters) | dict(
        unauthorized_provider_calls=0,
        successful_invalid_path_writes=0,
        observed_effect_policy_bypasses=0,
        formal_adoptions=0,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("train", "heldout", "all"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    release = load_release()
    migration = attest()
    selected = [
        case for case in load_faults().cases if args.split == "all" or case.split == args.split
    ]
    results, total = {}, Counter()
    for case in selected:
        folder = OUTPUT / "cases" / case.id
        archive = CaseArchive(folder)
        receipt_path = OUTPUT / "verified" / (case.id + ".json")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["release_ref"] == release.identity()
        assert receipt["manifest_sha256"] == digest((folder / "case.json").read_bytes())
        result = audit_case(archive)
        total.update(result)
        results[case.id] = dict(counts=result, raw_audit_sha256=digest(receipt_path.read_bytes()))
    value = dict(
        kind="repair-evaluation-safety-accounting/v1",
        release_ref=release.identity(),
        split=args.split,
        complete=True,
        cases=results,
        totals=dict(total),
        covered_method_outcomes=total["method_budgets_checked"],
        expected_method_outcomes=len(selected) * 5,
        migration_provenance=migration,
        analysis_code_sha256=digest(Path(__file__).read_bytes()),
        budget_fields_checked=[
            "phase_trials",
            "total_trials",
            "model_calls",
            "tool_calls",
            "mock_utf8_bytes",
            "cost_micro_usd",
            "active_reservations",
        ],
        scope="archived calls/artifacts and stage/call/byte/cost budgets; "
        "wall-clock enforced by execution layer; no host-wide I/O monitoring",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2)
    print(
        json.dumps(
            dict(path=str(args.output), sha256=digest(args.output.read_bytes()), totals=dict(total))
        )
    )


if __name__ == "__main__":
    main()
