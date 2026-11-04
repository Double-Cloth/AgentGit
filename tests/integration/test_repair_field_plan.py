"""实际删除新增 Plan 叶节点/依赖边，并用新候选重新确认。"""

import hashlib
from pathlib import Path

import pytest
from confirmation_evidence import capture_confirmation
from test_repair_confirmation import drive_confirmation, setup_confirmation

from agentgit.core.canonical import encode
from agentgit.core.models import parse_contract
from agentgit.core.repair_fields import RepairFieldState
from agentgit.core.research import evolve
from agentgit.repair.field_reduction import replay_fields

pytestmark = pytest.mark.integration


def test_plan_field_delete_leaf_fails_dependency_shrinks_and_final_ten_jobs_use_exact_dag(
    history, tmp_path
):
    policy = "field-plan-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    repo, study, research, source, reduced = setup_confirmation(
        history,
        tmp_path,
        mode="joint",
        policy=policy,
        field_policy="predefined-fields/v1",
        budget_transform=lambda b: evolve(b, quotas=evolve(b.quotas, reduce=16), max_trials=40),
    )
    fields = reduced["field_reduction"]
    assert reduced["status"] == "awaiting_samples" and fields["summary"]["status"] == "complete"
    assert fields["summary"]["accepted_fields"] == 2
    assert fields["summary"]["atomic_minimality"] == "one_minimal_under_fixture"
    assert fields["summary"]["field_minimality"] == "one_step_minimal_under_fixture"
    operations = [
        (t["intent"]["request"]["variant"]["operation"], t["result"]["execution"]["verdict"])
        for t in fields["trials"]
        if t["intent"]["request"]["variant"] is not None
    ]
    assert ("plan_leaf", "failed") in operations and ("plan_dependency", "passed") in operations
    state = parse_contract(encode(fields["state"]), RepairFieldState)
    replayed = replay_fields(state)
    patches = [replayed.patches[a.patch_ref] for a in replayed.candidate.atoms]
    plan = next(
        p.intervention.payload.plan for p in patches if p.intervention.operator == "PLAN_PATCH"
    )
    prompt = next(
        p.intervention.payload.prompt for p in patches if p.intervention.operator == "PROMPT_PATCH"
    )
    assert any(n.kind == "write_sources" for n in plan.nodes)
    assert not next(n for n in plan.nodes if n.kind == "write_sources").dependencies
    assert (
        reduced["usage"]["started"]["reduce"]
        == len(reduced["trials"]) + len(fields["trials"])
        <= 16
    )
    result = drive_confirmation(history, research, repo, policy=policy)
    assert result["status"] == "verified_under_fixture", result
    for trial in result["trials"]:
        assert trial["projection"]["plan"] == plan.model_dump(mode="json")
        assert trial["projection"]["config"]["prompt"] == prompt
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-durable-field-plan-six-plus-four/v1",
        environment="AGENTGIT_S10_FIELDS_PLAN_EVIDENCE",
    )
