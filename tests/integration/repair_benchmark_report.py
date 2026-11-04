"""以任务为单位汇总冻结评估，失败、未知及未完成复测均保留为分母。"""

import json
from collections import Counter

from repair_benchmark_protocol import METHODS

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairPatch
from agentgit.repair.changes import change_span
from agentgit.repair.space import patch_ref
from agentgit.repair.verification import wilson_95


def method_row(case, outcome):
    final = outcome.get("validation")
    verification = (final.get("verification") or {}) if final else {}
    latest = final or outcome.get("reduced") or outcome["source"]
    usage = latest["usage"]
    screened = outcome["source"]["status"] == "needs_confirmation"
    eligible = bool(verification.get("production_eligible", False))
    status = outcome["status"]
    verified = status == "verified_under_fixture" and eligible
    reduced = outcome.get("reduced") or {}
    lock = reduced.get("final_lock")
    atoms = lock["lock"]["candidate"]["atoms"] if lock else None
    reduction = reduced.get("reduction") or {}
    fields = (reduced.get("field_reduction") or {}).get("summary") or {}
    decision = outcome["source"].get("decision", {})
    attempted = (
        [trial["result"] for trial in final.get("trials", []) if trial["result"] is not None]
        if final
        else []
    )
    return dict(
        case_id=case.id,
        split=case.split,
        category=case.category,
        method=outcome["method"],
        status=status,
        screened_candidate=screened,
        finite_selected_space_exhausted=(
            status == "no_solution_in_space"
            and decision.get("unexplored_proposals") == 0
            and decision.get("determinate_failures") == decision.get("total_proposals")
            and decision.get("total_proposals", 0) > 0
        ),
        verified=verified,
        production_eligible=eligible,
        has_observation_patch=bool(verification.get("has_observation_patch", False)),
        confirm_attempted=sum(
            row["phase"] == "confirm" and not row["not_started"] for row in attempted
        ),
        confirm_passed=verification.get("confirm_passed", 0),
        regression_attempted=sum(
            row["phase"] == "regression" and not row["not_started"] for row in attempted
        ),
        regression_passed=verification.get("regression_passed", 0),
        regression_failed=sum(
            row["phase"] == "regression" and row["verdict"] == "failed" for row in attempted
        ),
        incomplete_results=sum(row["verdict"] == "incomplete" for row in attempted),
        confirm_regression_records=len(attempted),
        patch_count=len(atoms) if atoms is not None else None,
        screen_change_bytes=outcome["source"].get("decision", {}).get("selected_change_bytes"),
        pre_field_atomic_minimality=reduction.get("minimality", "not_tested"),
        atomic_minimality=fields.get(
            "atomic_minimality", reduction.get("minimality", "not_tested")
        ),
        atomic_minimality_scope=fields.get("minimality_scope")
        or reduction.get("minimality_scope", "not_tested"),
        field_minimality=fields.get("field_minimality", "not_tested"),
        minimality_stop_reason=fields.get("reason") or reduction.get("reason"),
        accepted_fields=fields.get("accepted_fields", 0),
        model_calls=usage["model_calls"],
        tool_calls=usage["tool_calls"],
        mock_utf8_bytes=usage["tokens"],
        cost_micro_usd=usage["cost_micro_usd"],
        phase_started=usage["started"],
        execution_wall_ms=outcome["execution_wall_ms"],
        ledger_wall_ms=usage["elapsed_wall_ms"],
        source_prefix_usage=outcome["source_origin"]["inherited_usage"],
        stop_reason=outcome.get("stopping_reason")
        or (
            verification.get("reasons")
            if final
            else reduction.get("reason") or outcome["source"].get("stop_reason")
        ),
    )


def patch_metrics(archive, outcome):
    lock = (outcome.get("reduced") or {}).get("final_lock")
    if not lock:
        return dict(
            final_prompt_bytes=None,
            prompt_change_bytes=None,
            plan_changed_nodes=None,
            observation_payload_bytes=None,
        )
    patches = {}
    for value in outcome["space"]["patches"]:
        patch = parse_contract(encode(value), RepairPatch)
        patches[patch_ref(patch)] = patch
    for row in archive.manifest["indexes"]["repair_field_patches"]:
        if row["study_id"] != outcome["study_id"]:
            continue
        value = json.loads(archive.get(row["variant_ref"], ObjectType.BLOB))
        patch = parse_contract(encode(value["patch"]), RepairPatch)
        assert patch_ref(patch) == row["effect_ref"]
        patches[row["effect_ref"]] = patch
    context = outcome["source_origin"]["context"]
    original_prompt = context["config"]["prompt"]
    snapshot = json.loads(archive.get(context["precondition"]["state_ref"], ObjectType.STATE))
    original_plan = snapshot["data"].get("plan")
    result = dict(
        final_prompt_bytes=0,
        prompt_change_bytes=0,
        plan_changed_nodes=0,
        observation_payload_bytes=0,
    )
    for atom in lock["lock"]["candidate"]["atoms"]:
        payload = patches[atom["patch_ref"]].intervention.payload
        if payload.kind == "PROMPT_PATCH":
            result["final_prompt_bytes"] += len(payload.prompt.encode())
            result["prompt_change_bytes"] += change_span(
                original_prompt.encode(), payload.prompt.encode()
            )
        elif payload.kind == "PLAN_PATCH":
            before = {n["key"]: n for n in original_plan["nodes"]} if original_plan else {}
            after = {n.key: n.model_dump(mode="json") for n in payload.plan.nodes}
            result["plan_changed_nodes"] += sum(
                before.get(k) != after.get(k) for k in set(before) | set(after)
            )
        else:
            call = next(
                c
                for c in archive.manifest["indexes"]["external_calls"]
                if c["id"] == payload.replacement_call_id
            )
            response = json.loads(archive.get(call["result_ref"], ObjectType.CALL))
            result["observation_payload_bytes"] += len(
                archive.get(response["response_ref"], ObjectType.BLOB)
            )
    return result


def summarize(rows):
    if len({(row["case_id"], row["method"]) for row in rows}) != len(rows):
        raise ValueError("同一任务/方法不能重复计为独立样本")
    groups = {}
    for split in ("train", "heldout"):
        for method in METHODS:
            subset = [row for row in rows if row["split"] == split and row["method"] == method]
            if not subset:
                continue
            successes = sum(row["verified"] for row in subset)
            interval = wilson_95(successes, len(subset))
            groups[split + "/" + method] = dict(
                task_count=len(subset),
                verified_tasks=successes,
                task_interval=interval.model_dump(mode="json"),
                statuses=dict(Counter(row["status"] for row in subset)),
                screened_tasks=sum(row["screened_candidate"] for row in subset),
                no_candidate_tasks=sum(not row["screened_candidate"] for row in subset),
                finite_selected_space_exhausted=sum(
                    row["finite_selected_space_exhausted"] for row in subset
                ),
                confirm_attempted=sum(row["confirm_attempted"] for row in subset),
                confirm_passed=sum(row["confirm_passed"] for row in subset),
                regression_attempted=sum(row["regression_attempted"] for row in subset),
                regression_passed=sum(row["regression_passed"] for row in subset),
                regression_failed=sum(row["regression_failed"] for row in subset),
                incomplete_results=sum(row["incomplete_results"] for row in subset),
                known_model_calls=sum(row["model_calls"] for row in subset),
                known_tool_calls=sum(row["tool_calls"] for row in subset),
                known_mock_utf8_bytes=sum(row["mock_utf8_bytes"] for row in subset),
                known_cost_micro_usd=sum(row["cost_micro_usd"] for row in subset),
                execution_wall_ms=sum(row["execution_wall_ms"] for row in subset),
                declared_scope="fixed synthetic fixture only; task-level Wilson is descriptive",
            )
    return groups
