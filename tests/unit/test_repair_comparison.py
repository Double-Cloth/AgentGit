"""候选排序须保留失败、未知与安全边界，不能凭低成本覆盖证据。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.repair import RepairCandidateComparison, RepairSearchDecision
from agentgit.core.research import evolve


def candidate(index=0, **changes):
    row = dict(
        proposal_id=str(uuid4()),
        proposal_key=f"{index + 1:064x}",
        trial_id=str(uuid4()),
        result_ref=f"{index + 100:064x}",
        ordinal=index,
        verdict="passed",
        category="oracle",
        determinate=True,
        patch_count=1,
        change_bytes=100,
        wall_ms=20,
        usage=dict(
            model_calls=1,
            tool_calls=0,
            tokens=100,
            cost_micro_usd=0,
            unit="mock_utf8_bytes",
            cost_basis="local_mock_free",
        ),
    )
    row.update(changes)
    return RepairCandidateComparison.model_validate(row)


def decision(rows):
    first = next((r for r in rows if r.verdict == "passed"), None)
    return dict(
        search_id=str(uuid4()),
        manifest_ref="a" * 64,
        policy_version="bounded-templates/v1",
        status="needs_confirmation",
        reason="screening_passed",
        total_proposals=len(rows),
        explored_proposals=len(rows),
        unexplored_proposals=0,
        excluded_atoms=0,
        passed_proposals=sum(r.verdict == "passed" for r in rows),
        determinate_failures=sum(r.determinate and r.verdict == "failed" for r in rows),
        result_refs=tuple(r.result_ref for r in rows),
        comparison=tuple(rows),
        selected_proposal_id=first.proposal_id,
        selected_proposal_key=first.proposal_key,
        selected_result_ref=first.result_ref,
        selected_change_bytes=first.change_bytes,
        usage={
            name: sum(getattr(r.usage, name) for r in rows)
            for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
        },
    )


def test_safe_success_precedes_smaller_failure_and_all_observations_are_retained():
    rows = [
        candidate(
            0,
            verdict="failed",
            category="safety",
            determinate=False,
            change_bytes=0,
            safety_violations=("forbidden",),
        ),
        candidate(1, verdict="failed", category="execution", determinate=False, change_bytes=1),
        candidate(2, verdict="failed", change_bytes=2),
        candidate(3, patch_count=2, change_bytes=500),
    ]
    ordered = sorted(rows, key=lambda r: r.rank_key())
    assert [r.ordinal for r in ordered] == [3, 2, 1, 0]
    parsed = RepairSearchDecision.model_validate(decision(ordered))
    assert parsed.selected_proposal_id == rows[3].proposal_id
    assert len(parsed.comparison) == 4 and parsed.determinate_failures == 1


def test_patch_count_then_change_then_measured_units_then_time_have_stable_priority():
    rows = [
        candidate(0, patch_count=2, change_bytes=1),
        candidate(1, change_bytes=101),
        candidate(2, wall_ms=None),
        candidate(3, wall_ms=30),
        candidate(4, wall_ms=10),
        candidate(5, wall_ms=10),
    ]
    assert [r.ordinal for r in sorted(rows, key=lambda r: r.rank_key())] == [4, 5, 3, 2, 1, 0]
    cheap = evolve(rows[2], usage=evolve(rows[2].usage, tokens=99))
    assert cheap.rank_key() < rows[4].rank_key()
    assert rows[4].rank_key() < rows[5].rank_key()


@pytest.mark.parametrize(
    "mutation", ["missing", "reversed", "duplicate", "result", "selected", "usage", "active"]
)
def test_forged_missing_or_reordered_comparisons_cannot_be_sealed(mutation):
    rows = (candidate(0), candidate(1, verdict="failed"))
    body = decision(rows)
    if mutation == "missing":
        body["comparison"] = rows[:1]
    elif mutation == "reversed":
        body["comparison"] = rows[::-1]
    elif mutation == "duplicate":
        body["comparison"] = (rows[0], rows[0])
    elif mutation == "result":
        body["result_refs"] = ("d" * 64, "e" * 64)
    elif mutation == "selected":
        body["selected_proposal_id"] = rows[1].proposal_id
    elif mutation == "usage":
        body["usage"]["tokens"] += 1
    elif mutation == "active":
        body["usage"].update(active=1, started={"screen": 1})
    with pytest.raises(ValidationError):
        RepairSearchDecision.model_validate(body)


@pytest.mark.parametrize(
    "change",
    [
        {"safety_violations": ("forbidden",)},
        {"determinate": False},
        {"category": "execution"},
        {"wall_ms": -1},
    ],
)
def test_unconfirmed_or_unsafe_trial_cannot_rank_as_success(change):
    with pytest.raises(ValidationError):
        candidate(**change)
