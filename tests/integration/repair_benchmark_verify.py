"""从整例 CAS 重算响应、硬验收和实际阶段成本；不信任报告的成功字段。"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from repair_benchmark_archive import CaseArchive

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.experiments import FrozenAssessment, ValidatorManifest
from agentgit.core.hybrid import FrozenHybridPolicy
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, ModelRequest
from agentgit.core.snapshots import StateSnapshot
from agentgit.experiments.hybrid import ObservationProvenance
from agentgit.experiments.validation import evaluate_frozen


def verify_case(folder: Path, model_source: Path):
    archive = CaseArchive(folder)
    archive.verify_objects()
    manifest = archive.manifest
    record, indexes = manifest["record"], manifest["indexes"]
    name = "frozen_case_model_" + hashlib.sha256(model_source.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, model_source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    model = module.FaultResearchModel(module.FaultCondition.model_validate(record["condition"]))
    assert model.fixture_ref == record["fixture_model_ref"]
    assert model.signature == record["model_signature"]
    assert record["source_before"] == record["source_after"] and "driver_failure" not in record
    calls = indexes["external_calls"]
    calls_by_run = {}
    for row in calls:
        calls_by_run.setdefault(row["run_id"], []).append(row)

    def content(ref, kind=ObjectType.BLOB):
        return archive.get(ref, kind)

    def body(ref, kind=ObjectType.BLOB):
        return json.loads(content(ref, kind))

    by_call = {row["id"]: row for row in calls}
    virtual_tools = set()
    for row in indexes["hybrid_observations"]:
        assert row["kind"] in ("recorded", "synthetic")
        policy = parse_contract(content(row["policy_ref"]), FrozenHybridPolicy)
        proof = parse_contract(content(row["provenance_ref"]), ObservationProvenance)
        selected = next(c for c in policy.calls if c.source_call_id == row["source_call_id"])
        call = by_call[row["call_id"]]
        saved = body(call["result_ref"], ObjectType.CALL)
        assert call["run_id"] == row["run_id"] and call["provider"].startswith("tool:")
        assert call["signature_digest"] == selected.intent.occurrence.signature_digest
        assert call["request_digest"] == selected.intent.request_ref
        assert proof.kind == row["kind"] and proof.selected_call_id == row["source_call_id"]
        expected = selected.replacement_response_ref or selected.saved.response_ref
        assert saved["response_ref"] == proof.response_ref == expected and saved["error"] is None
        assert (row["kind"] == "synthetic") == (selected.replacement_response_ref is not None)
        virtual_tools.add(row["call_id"])
    job_rows = {row["id"]: row for row in indexes["jobs"]}
    assert all(
        row["status"] in ("completed", "failed", "budget_exhausted", "cancelled", "needs_attention")
        and row["lease_until"] is None
        for row in job_rows.values()
    )
    allowed_goals = {record["public_input"]["goal"]["text"]}
    from repair_benchmark_regressions import regression_inputs

    allowed_goals.update(t.goal.text for t in regression_inputs())
    amounts = {}
    for identity, rows in calls_by_run.items():
        tokens = models = tools = 0
        for row in rows:
            assert row["provider"].startswith(
                ("llm:", "tool:corpus_search/", "tool:source_read/", "tool:artifact_write/")
            )
            if row["provider"].startswith("llm:"):
                intent = body(row["intent_ref"], ObjectType.CALL)
                outcome = body(row["result_ref"], ObjectType.CALL)
                payload = content(intent["request_ref"])
                request = parse_contract(payload, ModelRequest)
                assert request.state.goal.text in allowed_goals
                assert record["case_id"] not in request.state.goal.text
                assert request.available_tools == ("corpus_search", "source_read", "artifact_write")
                assert (
                    row["signature_digest"] == model.signature
                    and outcome["error"] is None
                    and outcome["complete"]
                )
                response = content(outcome["response_ref"])
                assert model.invoke(payload) == response
                if row["origin_call_id"] is None:
                    tokens += len(payload) + len(response)
                    models += 1
            elif row["origin_call_id"] is None and row["id"] not in virtual_tools:
                tools += 1
        amounts[identity] = dict(model_calls=models, tool_calls=tools, tokens=tokens)

    def assessment(value):
        item = parse_contract(encode(value), FrozenAssessment)
        snapshot = parse_contract(content(item.state_ref, ObjectType.STATE), StateSnapshot)
        state = parse_contract(encode(snapshot.data), AgentState)
        validator = parse_contract(content(item.validator_ref), ValidatorManifest)
        verdict, evaluation = evaluate_frozen(
            archive,
            validator,
            state,
            {file.path: content(file.object_ref) for file in snapshot.artifacts},
        )
        assert verdict == item.verdict and evaluation == item.evaluation
        return verdict

    assert assessment(record["source_assessment"]) == "failed"
    audits = {}
    for method, outcome in record["methods"].items():
        trial_ids = {
            row["id"]
            for row in indexes["repair_trials"]
            if row["search_id"] == outcome["search_id"]
        }
        dispatch = [row for row in indexes["repair_trial_jobs"] if row["id"] in trial_ids]
        study = outcome.get("study_id")
        if study:
            for prefix in ("repair_reduction", "repair_field", "repair_validation"):
                identities = {
                    row["id"] for row in indexes[prefix + "_trials"] if row["study_id"] == study
                }
                dispatch.extend(row for row in indexes[prefix + "_jobs"] if row["id"] in identities)
        run_ids = {job_rows[row["job_id"]]["run_id"] for row in dispatch}
        total = {
            key: sum(amounts.get(run, {}).get(key, 0) for run in run_ids)
            for key in ("model_calls", "tool_calls", "tokens")
        }
        latest = outcome.get("validation", outcome.get("reduced", outcome["source"]))
        assert all(total[key] == latest["usage"][key] for key in total), (
            method,
            total,
            latest["usage"],
        )
        assert latest["usage"]["active"] == 0 and latest["usage"]["cost_micro_usd"] == 0
        assert latest["usage"]["model_calls"] <= outcome["budget"]["max_model_calls"]
        assert latest["usage"]["tokens"] <= outcome["budget"]["max_tokens"]
        suite = next(
            row for row in indexes["repair_validation_suites"] if row["id"] == outcome["suite_id"]
        )
        assert all(
            suite["created_at"] <= row["created_at"]
            for row in indexes["repair_trials"]
            if row["search_id"] == outcome["search_id"]
        )
        confirmed = regressed = 0
        if "validation" in outcome:
            final = outcome["validation"]
            for trial in final["trials"]:
                receipt = trial["result"]
                if receipt is None:
                    continue
                verdict = assessment(receipt["assessment"])
                assert verdict == receipt["verdict"]
                physical = amounts.get(receipt["current_run_id"], {})
                assert (
                    physical["model_calls"]
                    == receipt["fresh_model_calls"]
                    == receipt["usage"]["model_calls"]
                )
                assert physical["tokens"] == receipt["usage"]["tokens"]
                assert all(
                    value <= outcome["budget"]["trial"]["max_" + key]
                    for key, value in physical.items()
                )
                confirmed += receipt["phase"] == "confirm" and verdict == "passed"
                regressed += receipt["phase"] == "regression" and verdict == "passed"
            assert final["verification"]["confirm_passed"] == confirmed
            assert final["verification"]["regression_passed"] == regressed
            if outcome["status"] == "verified_under_fixture":
                assert confirmed >= 5 and regressed == 4
        audits[method] = dict(
            status=outcome["status"],
            actual=total,
            confirm_passed=confirmed,
            regression_passed=regressed,
            jobs=len(run_ids),
        )
    assert not indexes["proposal_reviews"] and not indexes["repair_saved_experiments"]
    return dict(
        path=str(folder),
        raw_bytes=manifest["raw_bytes"],
        objects=len(manifest["objects"]),
        model_source_sha256=hashlib.sha256(model_source.read_bytes()).hexdigest(),
        methods=audits,
        all_physical={
            key: sum(row[key] for row in amounts.values())
            for key in ("model_calls", "tool_calls", "tokens")
        },
    )
