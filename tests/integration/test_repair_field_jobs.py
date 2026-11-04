"""字段缩短后以独立实际 Job 复查必要性，并确认确切派生内容。"""

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
from confirmation_evidence import capture_confirmation
from test_repair_confirmation import drive_confirmation, setup_confirmation
from test_repair_confirmation_hybrid import setup_hybrid_confirmation
from test_repair_worker import drive

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairSearchManifest
from agentgit.core.repair_fields import RepairFieldState
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.repair.field_reduction import replay_fields
from agentgit.services.repair_fields import RepairFieldService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.repair_studies import RepairStudyService

pytestmark = pytest.mark.integration


def test_prompt_field_shrinks_rechecks_and_ten_new_jobs_use_exact_locked_derived_content(
    history, tmp_path
):
    policy = "field-fixture-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    repo, study, research, source, reduced = setup_confirmation(
        history, tmp_path, policy=policy, field_policy="predefined-fields/v1"
    )
    assert reduced["status"] == "awaiting_samples", reduced
    fields = reduced["field_reduction"]
    assert fields["summary"]["status"] == "complete"
    assert fields["summary"]["accepted_fields"] == 1
    assert fields["summary"]["atomic_minimality"] == "one_minimal_under_fixture"
    assert fields["summary"]["field_minimality"] == "one_step_minimal_under_fixture"
    assert [trial["intent"]["request"]["kind"] for trial in fields["trials"]] == [
        "field",
        "atomic_recheck",
        "field",
    ]
    assert [trial["result"]["execution"]["verdict"] for trial in fields["trials"]] == [
        "passed",
        "failed",
        "failed",
    ]
    state = parse_contract(encode(fields["state"]), RepairFieldState)
    replayed = replay_fields(state)
    final = replayed.patches[replayed.candidate.atoms[0].patch_ref]
    original = next(p for p in state.plan.patches if p.intervention.operator == "PROMPT_PATCH")
    assert len(final.intervention.payload.prompt.encode()) < len(
        original.intervention.payload.prompt.encode()
    )
    assert "仅引用本次已读取" in final.intervention.payload.prompt
    assert reduced["final_lock"]["lock"]["candidate"] == replayed.candidate.model_dump(mode="json")
    assert reduced["usage"]["started"]["reduce"] == 4
    result = drive_confirmation(history, research, repo, policy=policy)
    assert result["status"] == "verified_under_fixture", result
    assert (
        result["verification"]["confirm_passed"] == 6
        and result["verification"]["regression_passed"] == 4
    )
    for trial in result["trials"]:
        assert trial["projection"]["config"]["prompt"] == final.intervention.payload.prompt
        report = history.objects.get_json(trial["result"]["provenance_ref"], ObjectType.BLOB)
        assert report["model_calls"]
    assert RepairStudyService(history).view(repo, study)["field_reduction"] == fields
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-durable-field-six-plus-four/v1",
        environment="AGENTGIT_S10_FIELDS_CONFIRMATION_EVIDENCE",
    )
    # 相同摘要不代表相同 study；兄弟搜索可以合法复用同一冻结来源。
    manifest = parse_contract(encode(source["manifest"]), RepairSearchManifest)
    sibling = RepairSearchService(history).submit(
        manifest.space,
        manifest.budget,
        manifest.intervention_ids,
        actor="operator",
        key="sibling-source",
    )
    sibling_search = UUID(sibling["search_id"])
    other_source = drive(research, repo, sibling_search)
    assert other_source["manifest"] == source["manifest"]
    other = RepairStudyService(history).submit(
        repo, NewRepairStudy(search_id=str(sibling_search)), actor="operator", key="sibling"
    )
    candidate = replayed.candidate
    service = RepairStudyService(history)
    for other_study in (None, UUID(other["study_id"])):
        with pytest.raises(AgentGitError) as error:
            service._applied(repo, manifest, candidate, study_id=other_study)
        assert error.value.code == ErrorCode.FORBIDDEN
    with history.engine.connect() as connection:
        resolved, _ = RepairFieldService(history).resolve_patch(
            connection, repo, study, manifest, candidate.atoms[0].patch_ref
        )
        assert resolved == final


def test_hybrid_field_jobs_own_policy_and_confirm_exact_shortened_content(history, tmp_path):
    repo, study, research, source, reduced = setup_hybrid_confirmation(
        history, tmp_path, field_policy="predefined-fields/v1"
    )
    fields = reduced["field_reduction"]
    assert fields["summary"]["accepted_fields"] == 1
    assert fields["summary"]["field_minimality"] == "one_step_minimal_under_fixture"
    state = parse_contract(encode(fields["state"]), RepairFieldState)
    replayed = replay_fields(state)
    final = replayed.patches[replayed.candidate.atoms[0].patch_ref]
    for trial in fields["trials"]:
        result = trial["result"]
        receipt = result["execution"]
        assert trial["intent"]["hybrid_ref"]
        report = history.objects.get_json(receipt["replay_report_ref"], ObjectType.BLOB)
        assert report["mode"] == "hybrid"
        assert result["fresh_model_calls"] == receipt["usage"]["model_calls"] > 0
    result = drive_confirmation(history, research, repo)
    assert result["status"] == "verified_under_fixture", result
    for trial in result["trials"]:
        assert trial["projection"]["config"]["prompt"] == final.intervention.payload.prompt
        report = history.objects.get_json(trial["result"]["replay_report_ref"], ObjectType.BLOB)
        if trial["result"]["phase"] == "confirm":
            assert report["mode"] == "hybrid"
            assert report["actual"]["recorded_observations"] == 1
        else:
            assert report["mode"] == "live" and trial["projection"]["hybrid_ref"] is None
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-durable-field-hybrid-six-plus-four/v1",
        environment="AGENTGIT_S10_FIELDS_HYBRID_EVIDENCE",
    )
