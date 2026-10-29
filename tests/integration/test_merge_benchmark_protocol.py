"""正式统计的拒绝/缺失边界；无需数据库，不把未知失败计成成功阻断。"""

import copy
import json

import pytest
from merge_benchmark_methods import assess, three_way
from merge_benchmark_release import RELEASE, release_digest, verify_release


def blocked():
    return dict(id="M20", group="fact", expected="block", errors=["validation"])


@pytest.mark.parametrize("decision", ["candidate", "conflict", "input_rejected"])
def test_three_outcomes_have_distinct_conflict_and_schema_denominators(decision):
    result = assess(
        blocked(),
        dict(decision=decision, conflicts=[{"kind": "fixture"}], error={"code": "validation"}),
    )
    assert result["safe_rejection"] == (decision != "candidate")
    assert result["conflict_detected"] == (decision == "conflict")
    assert result["input_rejected"] == (decision == "input_rejected")
    assert result["false_accept"] == (decision == "candidate")


def test_unexpected_storage_error_is_incomplete_and_never_safe_rejection():
    result = assess(blocked(), dict(decision="input_rejected", error={"code": "object_corrupt"}))
    assert result["incomplete"] and not result["safe_rejection"]
    assert not result["matches_expectation"]


@pytest.mark.parametrize(
    "record",
    [
        dict(decision="missing"),
        dict(decision="conflict", conflicts=[]),
        dict(decision="input_rejected", error=None),
    ],
)
def test_missing_or_empty_evidence_cannot_be_scored_as_success(record):
    with pytest.raises(AssertionError):
        assess(blocked(), record)


def clean_example():
    expected = dict(
        artifacts={"report.md": "report"},
        goal={"text": "fixture"},
        nodes={"read": {"key": "read"}},
        memory={"source": {"fixture": True}},
        tools=["read"],
        limits={"calls": 1},
        read_call_ids={"ours-read", "theirs-read"},
    )
    record = dict(
        decision="candidate",
        artifacts=[dict(path="report.md", text="report", status="merged")],
        state=dict(
            goal=expected["goal"],
            plan=dict(nodes=list(expected["nodes"].values())),
            working_memory=expected["memory"],
            constraints=dict(tools=expected["tools"], limits=expected["limits"]),
        ),
        config=None,
        read_call_ids=sorted(expected["read_call_ids"]),
    )
    return expected, record


def test_same_artifacts_without_state_do_not_count_as_full_content_integration():
    expected, record = clean_example()
    record["state"] = None
    result = assess(dict(expected="accept"), record, expected)
    assert result["clean_artifacts_correct"] and not result["clean_content_correct"]


def test_complete_reports_cannot_hide_lost_distinct_branch_read_proof():
    expected, record = clean_example()
    assert assess(dict(expected="accept"), record, expected)["matches_expectation"]
    changed = copy.deepcopy(record)
    changed["read_call_ids"] = ["ours-read"]
    assert not assess(dict(expected="accept"), changed, expected)["matches_expectation"]


def test_independent_three_way_oracle_does_not_equate_bool_and_integer():
    with pytest.raises(AssertionError):
        three_way(False, 0, True)


@pytest.mark.parametrize("change", ["digest", "driver", "case", "source", "file"])
def test_release_rejects_changed_protocol_or_omitted_actual_inputs(tmp_path, change):
    value = json.loads(RELEASE.read_text("utf-8"))
    if change == "digest":
        value["max_merge_model_calls"] = 1
    elif change == "driver":
        del value["files"]["tests/integration/test_merge_benchmark_evaluation.py"]
    elif change == "case":
        value["cases"] = value["cases"][:-1]
    elif change == "source":
        value["code_revision"] = "source-sha256:" + "0" * 64
    else:
        value["files"]["tests/integration/merge_benchmark_methods.py"] = "0" * 64
    if change != "digest":
        value["release_digest"] = release_digest(value)
    temporary = tmp_path / "release.json"
    temporary.write_text(json.dumps(value), "utf-8")
    with pytest.raises(ValueError):
        verify_release(temporary)
