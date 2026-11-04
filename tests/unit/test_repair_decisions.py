"""停止报告只允许在已声明空间内作有证据的结论。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.repair import RepairSearchDecision


def body():
    return dict(
        search_id=str(uuid4()),
        manifest_ref="a" * 64,
        policy_version="bounded-templates/v1",
        status="no_solution_in_space",
        reason="declared_space_exhausted",
        total_proposals=2,
        explored_proposals=2,
        unexplored_proposals=0,
        passed_proposals=0,
        determinate_failures=2,
        excluded_atoms=4,
        result_refs=("b" * 64, "c" * 64),
        usage={},
    )


def test_no_solution_is_explicitly_local_and_never_a_verified_fix():
    decision = RepairSearchDecision.model_validate(body())
    assert decision.excluded_atoms == 4 and not decision.global_optimum
    assert decision.confirmation == "not_run" and decision.minimality == "not_tested"


@pytest.mark.parametrize(
    "change",
    [
        {"explored_proposals": 1, "unexplored_proposals": 1, "determinate_failures": 1},
        {"determinate_failures": 1},
        {"passed_proposals": 1, "determinate_failures": 1},
        {"total_proposals": 0, "explored_proposals": 0, "determinate_failures": 0},
        {"global_optimum": True},
        {"confirmation": "verified"},
        {"status": "needs_confirmation"},
        {"selected_result_ref": "d" * 64},
    ],
)
def test_incomplete_unsafe_or_unconfirmed_evidence_cannot_claim_stronger_conclusion(change):
    value = body()
    value.update(change)
    with pytest.raises(ValidationError):
        RepairSearchDecision.model_validate(value)
