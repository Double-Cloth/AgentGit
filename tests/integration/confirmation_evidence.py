"""从原始 CAS 字节独立核算验证用量，并保存完整阶段索引。"""

import base64
import json
import os
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from diagnosis_evidence import pack_repository_evidence

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.management import public
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


def capture_confirmation(history, result, source, reduced, *, case, environment):
    repository = UUID(source["manifest"]["space"]["binding"]["repository_id"])
    source_run = UUID(source["manifest"]["space"]["binding"]["failed_run_id"])
    graph = BehaviorService(history).trace(repository, source_run).model_dump(mode="json")
    evidence = dict(
        repository_id=str(repository),
        failed_run_id=str(source_run),
        reference_run_id=str(source_run),
        reference_role="source_alias_for_archive_only",
        case_id=case,
        graph=graph,
        reference_graph=graph,
    )
    raw = pack_repository_evidence(history, evidence)
    with history.engine.connect() as connection:
        indexes = {
            name: [
                public(dict(row))
                for row in connection.execute(
                    sa.select(TABLES[name]).where(TABLES[name].c.repository_id == repository)
                ).mappings()
            ]
            for name in (
                "repair_searches",
                "repair_trials",
                "repair_trial_jobs",
                "repair_trial_results",
                "repair_studies",
                "repair_study_jobs",
                "repair_reduction_trials",
                "repair_reduction_jobs",
                "repair_reduction_results",
                "repair_field_states",
                "repair_field_patches",
                "repair_field_trials",
                "repair_field_jobs",
                "repair_field_results",
                "repair_field_waits",
                "repair_field_summaries",
                "repair_adoption_proposals",
                "repair_review_sessions",
                "repair_saved_experiments",
                "proposal_reviews",
                "agent_configs",
                "tasks",
                "task_nodes",
                "repair_validation_suites",
                "repair_validation_samples",
                "repair_validation_openings",
                "repair_validation_trials",
                "repair_validation_jobs",
                "repair_validation_results",
                "repair_validation_waits",
                "repair_validation_summaries",
                "repair_study_locks",
                "repair_study_events",
                "jobs",
                "replay_requests",
            )
        }
    values = {
        ref: base64.b64decode(item["base64"], validate=True) for ref, item in raw["objects"].items()
    }
    for ref, payload in values.items():
        assert object_digest(ObjectType(raw["objects"][ref]["object_type"]), payload) == ref
    assert sum(map(len, values.values())) == raw["raw_bytes"]
    samples = {row["id"]: row for row in indexes["repair_validation_samples"]}
    trials = {row["id"]: row for row in indexes["repair_validation_trials"]}
    replays = {row["job_id"]: row for row in indexes["replay_requests"]}
    openings = {row["id"]: row for row in indexes["repair_validation_openings"]}
    actual_tokens, new_calls, responses = 0, set(), []
    for trial in result["trials"]:
        receipt = trial["result"]
        proof = json.loads(values[receipt["provenance_ref"]])
        assert (
            proof["job_id"] == receipt["job_id"]
            and proof["current_run_id"] == receipt["current_run_id"]
        )
        assert receipt["protocol_ref"] == openings[result["study_id"]]["plan_ref"]
        assert receipt["candidate_ref"] == reduced["final_lock"]["lock_ref"]
        sample = samples[trials[trial["trial_id"]]["sample_id"]]
        ticket = json.loads(values[sample["generation_ref"]])
        assert ticket["generation_id"] == receipt["sample_id"]
        assert replays[receipt["job_id"]]["source_run_id"] == ticket["source_run_id"]
        assert replays[receipt["job_id"]]["from_commit_id"] == ticket["from_commit"]
        assert set(proof["run_ids"]).isdisjoint(reduced["final_lock"]["lock"]["prior_run_ids"])
        assert ticket["source_run_id"] not in proof["run_ids"]
        own = [call for call in proof["model_calls"] if call["origin_call_id"] is None]
        size = 0
        for call in own:
            assert call["call_id"] not in new_calls
            new_calls.add(call["call_id"])
            intent = json.loads(values[call["intent_ref"]])
            response = json.loads(values[call["result_ref"]])
            assert response["complete"] and response["error"] is None
            size += len(values[intent["request_ref"]]) + len(values[response["response_ref"]])
            responses.append(response["response_ref"])
        assert len(own) == receipt["fresh_model_calls"] == receipt["usage"]["model_calls"]
        assert size == receipt["usage"]["tokens"] and receipt["usage"]["unit"] == "mock_utf8_bytes"
        assert receipt["usage"]["cost_micro_usd"] == 0
        actual_tokens += size
    assert len(set(responses)) < len(responses), "相同输出也须保留为不同实际调用，不能按输出筛样"
    assert result["usage"]["tokens"] == reduced["usage"]["tokens"] + actual_tokens
    assert result["verification"]["known_mock_utf8_bytes"] == actual_tokens
    bundle = dict(
        schema_version=1,
        case_id=case,
        result=result,
        reduced=reduced,
        source=source,
        indexes=indexes,
        evidence=evidence,
        raw_repository=raw,
        raw_audit=dict(
            model_calls=len(new_calls),
            mock_utf8_bytes=actual_tokens,
            distinct_response_refs=len(set(responses)),
            response_records=len(responses),
        ),
    )
    path = os.environ.get(environment)
    if path:
        with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(bundle, handle, ensure_ascii=False, indent=2)
    assert len(encode(result)) < 8 * 1024 * 1024
    return bundle
