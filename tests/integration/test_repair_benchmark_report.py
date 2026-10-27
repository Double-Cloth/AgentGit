"""研究统计不删失败或将确认次数当任务数，观测与未确认也不能算正式修复。"""

from types import SimpleNamespace

import pytest
from repair_benchmark_report import method_row, summarize


def outcome(status="no_solution_in_space", *, eligible=False, observation=False, passed=0):
    usage = dict(
        model_calls=1,
        tool_calls=1,
        tokens=100,
        cost_micro_usd=0,
        elapsed_wall_ms=2,
        started=dict(screen=1, reduce=0, confirm=0, regression=0),
    )
    value = dict(
        method="repair",
        status=status,
        source=dict(
            status="needs_confirmation" if passed else status,
            usage=usage,
            decision={},
            stop_reason="fixture",
        ),
        execution_wall_ms=3,
        source_origin=dict(inherited_usage=dict(model_calls=7, tokens=1000)),
    )
    if passed:
        value["validation"] = dict(
            usage=usage,
            trials=[],
            verification=dict(
                production_eligible=eligible,
                has_observation_patch=observation,
                confirm_passed=passed,
                regression_passed=4,
                reasons=[],
            ),
        )
    return value


def case(identity):
    return SimpleNamespace(id=identity, split="train", category="single")


def test_task_denominator_includes_failed_budget_unknown_and_unconfirmed_results():
    rows = [method_row(case("R01"), outcome("verified_under_fixture", eligible=True, passed=6))]
    for index, status in enumerate(
        ("budget_exhausted", "needs_attention", "no_solution_in_space", "awaiting_samples"), 2
    ):
        rows.append(method_row(case(f"R{index:02}"), outcome(status)))
    result = summarize(rows)["train/repair"]
    assert result["task_count"] == 5 and result["verified_tasks"] == 1
    assert result["task_interval"]["samples"] == 5 and result["task_interval"]["successes"] == 1
    assert result["confirm_passed"] == 6
    assert result["known_model_calls"] == 5 and result["known_mock_utf8_bytes"] == 500


def test_observation_verification_is_not_a_production_eligible_task_success():
    row = method_row(
        case("R01"), outcome("verified_under_fixture", eligible=False, observation=True, passed=6)
    )
    assert not row["verified"] and row["has_observation_patch"] and row["confirm_passed"] == 6
    assert summarize([row])["train/repair"]["verified_tasks"] == 0


def test_duplicate_task_method_cannot_inflate_statistics():
    row = method_row(case("R01"), outcome())
    with pytest.raises(ValueError):
        summarize([row, row])


def test_budget_stop_cannot_be_labeled_as_exhaustive_no_solution():
    complete = outcome("no_solution_in_space")
    complete["source"]["decision"] = dict(
        total_proposals=2, determinate_failures=2, unexplored_proposals=0
    )
    assert method_row(case("R01"), complete)["finite_selected_space_exhausted"]
    complete["status"] = "budget_exhausted"
    assert not method_row(case("R01"), complete)["finite_selected_space_exhausted"]
    complete["status"] = "no_solution_in_space"
    complete["source"]["decision"]["determinate_failures"] = 1
    assert not method_row(case("R01"), complete)["finite_selected_space_exhausted"]


def test_field_counts_and_checkpoint_usage_keep_separate_units():
    value = outcome("verified_under_fixture", eligible=True, passed=6)
    value["reduced"] = dict(field_reduction=dict(summary=dict(accepted_fields=2)))
    row = method_row(case("R01"), value)
    assert row["accepted_fields"] == 2 and row["model_calls"] == 1
    assert row["source_prefix_usage"]["model_calls"] == 7
    assert row["mock_utf8_bytes"] == 100


def test_indeterminate_deletion_keeps_pending_fields_and_confirmation_as_incomplete():
    value = outcome("needs_review")
    value["source"]["status"] = "needs_confirmation"
    value["reduced"] = dict(
        usage=value["source"]["usage"],
        reduction=dict(reason="indeterminate_deletions", minimality="not_proven"),
        field_reduction=None,
        final_lock=None,
    )
    row = method_row(case("R20"), value)
    assert row["screened_candidate"] and not row["verified"]
    assert row["stop_reason"] == "indeterminate_deletions"
    assert row["accepted_fields"] == row["confirm_attempted"] == row["regression_attempted"] == 0
    assert row["patch_count"] is None and row["atomic_minimality"] == "not_proven"
    assert summarize([row])["train/repair"]["statuses"] == {"needs_review": 1}
