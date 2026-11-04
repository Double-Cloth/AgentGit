"""离线分析、基线和严格分母；只用合成数据与开发集做工程核验。"""

import base64
import hashlib
import json
from pathlib import Path

import evaluate_diagnosis as runner
import pytest
from diagnosis_dataset import load_dataset, run_case
from diagnosis_evaluation import (
    FrozenEvidence,
    aggregate,
    analyze_evidence,
    exact_mean,
    score_targets,
)
from diagnosis_evidence import pack_repository_evidence
from test_debug_reports import forbid_execution

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.diagnosis import EventReference, TraceGraph, TraceNode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.snapshots import EventPayload
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration
REPO = "00000000-0000-4000-8000-000000000001"
RUN = "00000000-0000-4000-8000-000000000002"


def minimal():
    nodes, events, refs, contents = [], [], [], {}
    for seq, clock in ((1, "2026-10-24T00:00:02+00:00"), (2, "2026-10-24T00:00:01+00:00")):
        raw = encode(
            EventPayload(data={"error": {"code": "tool_error", "message": "合成错误"}}).model_dump(
                mode="json"
            )
        )
        digest = object_digest(ObjectType.EVENT, raw)
        event = EventReference(run_id=RUN, seq=seq, event_id=seq)
        nodes.append(
            TraceNode(
                event=event,
                event_type="run.stopped",
                logical_step_id=f"state-{seq}",
                phase="run.stopped",
                payload_ref=digest,
            )
        )
        events.append(
            {
                "repository_id": REPO,
                "run_id": RUN,
                "seq": seq,
                "id": seq,
                "payload_ref": digest,
                "recorded_at": clock,
            }
        )
        refs.append(
            {
                "repository_id": REPO,
                "owner_type": "event",
                "owner_id": event.key,
                "role": "payload",
                "object_id": digest,
            }
        )
        contents[digest] = {
            "object_type": "event",
            "byte_length": len(raw),
            "base64": base64.b64encode(raw).decode(),
        }
    graph = TraceGraph(
        repository_id=REPO,
        run_id=RUN,
        anchor_commit=None,
        as_of_seq=2,
        run_status="failed",
        scope_complete=False,
        nodes=tuple(nodes),
    )
    bundle = {
        "test_only": True,
        "repository_id": REPO,
        "raw_bytes": sum(item["byte_length"] for item in contents.values()),
        "objects": contents,
        "indexes": {
            "events": events,
            "object_refs": refs,
            "runs": [
                {
                    "repository_id": REPO,
                    "id": RUN,
                    "status": "failed",
                    "checkpoint_commit_id": None,
                    "last_event_seq": 2,
                }
            ],
        },
    }
    return graph, bundle


def score_input(graph, *, status="candidates_available", tied=1):
    event = graph.nodes[0].event.model_dump(mode="json")
    return {
        "diagnosis": {
            "status": status,
            "candidates": [{"event": event, "rank": 1, "tied_count": tied}],
            "observed_anomalies": [event],
        },
        "baseline_first_failure": event,
    }


def labels(graph):
    event = graph.nodes[0].event.model_dump(mode="json")
    return {"injection": event, "first_visible": event, "repairable_decisions": [event]}


def test_boundary_tie_does_not_choose_a_favorable_event_id_for_top1():
    graph, _ = minimal()
    result = score_targets(score_input(graph, tied=2), labels(graph), graph)
    assert not result["injection_point"]["top1_hit"]
    assert result["injection_point"]["top1_boundary_ambiguity"]
    assert result["injection_point"]["top3_hit"]


def test_corpus_target_is_unsupported_for_exact_event_metric_and_not_silently_relabelled():
    graph, _ = minimal()
    target = {**labels(graph), "injection": {"kind": "frozen_input", "source_id": "fixture"}}
    result = score_targets(score_input(graph), target, graph)
    assert not result["injection_point"]["target_supported"]
    assert not result["injection_point"]["top3_hit"]
    assert result["repairable_decision"]["top3_hit"]


def test_fallback_without_verified_location_is_not_counted_as_localization_hit():
    graph, _ = minimal()
    result = score_targets(score_input(graph, status="insufficient_evidence"), labels(graph), graph)
    assert not result["repairable_decision"]["top3_hit"]


def test_bad_label_id_is_rejected_instead_of_counted_as_a_correct_match():
    graph, _ = minimal()
    target = {**labels(graph), "first_visible": {**labels(graph)["first_visible"], "event_id": 99}}
    with pytest.raises(AgentGitError):
        score_targets(score_input(graph), target, graph)


def test_error_case_stays_in_denominator_and_missing_steps_stay_unknown():
    graph, _ = minimal()
    scores = score_targets(score_input(graph), labels(graph), graph)
    rows = [
        {
            "with_reference": {
                "status": "analyzed",
                "scores": scores,
                "analysis": {**score_input(graph), "effort": {"distance_node_visits": 4}},
            }
        },
        {"with_reference": {"status": "evidence_error"}},
    ]
    result = aggregate(rows, "with_reference")
    assert result["scheduled"] == 2 and result["unlocated"] == 1
    assert result["targets"]["injection_point"]["denominator"] == 2
    assert result["targets"]["injection_point"]["top3_hit"] == 1
    assert result["effort"]["distance_node_visits"] == {
        "known": 1,
        "missing": 1,
        "mean": {"numerator": "4", "denominator": "1"},
    }
    assert exact_mean([1, 2])["mean"] == {"numerator": "3", "denominator": "2"}


def test_time_baseline_uses_recorded_time_and_analysis_never_receives_labels():
    graph, bundle = minimal()
    evidence = FrozenEvidence(bundle, graph, None)
    assert evidence.first_explicit_failure(graph).seq == 2
    a = analyze_evidence(graph, None, bundle)
    bundle["gold"] = "伪造根因"
    b = analyze_evidence(graph, None, bundle)
    assert a["diagnosis"] == b["diagnosis"]
    assert a["effort"]["model_calls"] == a["effort"]["tool_calls"] == 0


@pytest.mark.parametrize("case_id", ["D04", "D06"])
def test_offline_and_online_diagnosis_agree_for_verified_development_evidence(
    history, tmp_path, monkeypatch, case_id
):
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.id == case_id and case.split == "train")
    proof = run_case(history, tmp_path / "case", case, dataset)
    bundle = pack_repository_evidence(history, proof)
    manager = ManagementService(ResearchService(history, tmp_path / "manager"))
    failed = parse_contract(encode(proof["graph"]), TraceGraph)
    reference = parse_contract(encode(proof["reference_graph"]), TraceGraph)
    from uuid import UUID

    forbid_execution(monkeypatch)
    online = manager.debug_reports.compile(
        UUID(proof["repository_id"]),
        NewDebugReport(
            failed_run_id=proof["failed_run_id"], reference_run_id=proof["reference_run_id"]
        ),
    )
    monkeypatch.setattr(
        history.engine, "connect", lambda *args, **kwargs: pytest.fail("离线评估不得连接数据库")
    )
    offline = analyze_evidence(failed, reference, bundle)
    assert offline["diagnosis"] == online.diagnosis.model_dump(mode="json")
    assert offline["effort"]["distance_node_visits"] > 0
    assert offline["effort"]["model_calls"] == offline["effort"]["tool_calls"] == 0


def fake_release(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[2]
    for relative in (
        "tests/fixtures/diagnosis/v1/cases.json",
        "tests/integration/diagnosis_dataset.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((project / relative).read_bytes())
    dataset = load_dataset()
    code = tmp_path / "code.py"
    code.write_text("固定评估替身", encoding="utf-8")
    rule = {
        "rules_digest": "a" * 64,
        "source_revision": runner.source_revision(),
        "implementation_sha256": {"code.py": hashlib.sha256(code.read_bytes()).hexdigest()},
        "dataset": {
            "cases_sha256": hashlib.sha256(
                (tmp_path / "tests/fixtures/diagnosis/v1/cases.json").read_bytes()
            ).hexdigest(),
            "fixture_sha256": hashlib.sha256(
                (tmp_path / "tests/integration/diagnosis_dataset.py").read_bytes()
            ).hexdigest(),
            "train": [case.id for case in dataset.cases[:12]],
            "heldout": [case.id for case in dataset.cases[12:]],
        },
    }
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(rule), encoding="utf-8")
    summary = {
        "cases": [
            {
                "id": case.id,
                "split": case.split,
                "proof_sha256": "b" * 64,
                "bundle_sha256": "c" * 64,
            }
            for case in dataset.cases
        ],
        "source_revision": "frozen-fixture",
        "case_construction_cost": {},
    }
    input_file = tmp_path / "summary.json"
    input_file.write_text(json.dumps(summary), encoding="utf-8")
    release = {
        "schema_version": 1,
        "status": "frozen_before_heldout_evaluation",
        "implementation_sha256": {"code.py": hashlib.sha256(code.read_bytes()).hexdigest()},
        "rules_file": "rules.json",
        "rules_file_sha256": hashlib.sha256(rules_file.read_bytes()).hexdigest(),
        "input_summary_file": "summary.json",
        "input_summary_sha256": hashlib.sha256(input_file.read_bytes()).hexdigest(),
    }
    release_file = tmp_path / "release.json"
    release_file.write_text(json.dumps(release), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    return release_file


@pytest.mark.parametrize("change", ["none", "code", "spec", "summary", "missing_case"])
def test_evaluation_release_enforces_code_data_and_complete_twenty_case_inputs(
    tmp_path, monkeypatch, change
):
    path = fake_release(tmp_path, monkeypatch)
    if change == "none":
        assert len(runner.validate_release(path)[2].cases) == 20
        return
    relative = {
        "code": "code.py",
        "spec": "tests/fixtures/diagnosis/v1/cases.json",
        "summary": "summary.json",
        "missing_case": "summary.json",
    }[change]
    target = tmp_path / relative
    if change == "missing_case":
        value = json.loads(target.read_bytes())
        value["cases"].pop()
        target.write_text(json.dumps(value), encoding="utf-8")
        release = json.loads(path.read_bytes())
        release["input_summary_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        path.write_text(json.dumps(release), encoding="utf-8")
    else:
        target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(AgentGitError):
        runner.validate_release(path)


def test_missing_all_case_files_keeps_twenty_error_rows_and_never_passes_gate(
    tmp_path, monkeypatch
):
    release = fake_release(tmp_path, monkeypatch)
    result = runner.evaluate(release, tmp_path / "missing-evidence")
    assert len(result["rows"]) == result["case_count"] == 20
    for mode in runner.MODES:
        assert all(row[mode]["status"] == "evidence_error" for row in result["rows"])
        assert result["groups"][mode]["all"]["targets"]["injection_point"]["denominator"] == 20
        assert result["groups"][mode]["all"]["unlocated"] == 20
    assert not result["formal_s8_t04_passed"] and not result["formal_s8_t05_passed"]
