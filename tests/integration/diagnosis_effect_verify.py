"""只读重算效果归档的同条件响应、硬验收和物理量，不执行真实工具。"""

import argparse
import base64
import importlib.util
import json
import sys
from collections import Counter
from fractions import Fraction
from types import SimpleNamespace

from diagnosis_effect_execution_v3 import OUTPUT, load_protocol, sha
from diagnosis_effect_policy import fixed_payload
from diagnosis_evidence import verify_repository_evidence

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.debug_reports import DebugReport
from agentgit.core.errors import AgentGitError
from agentgit.core.experiments import ValidatorManifest
from agentgit.core.interventions import PlanPatch, PromptPatch
from agentgit.core.models import parse_contract
from agentgit.core.paths import validate_artifact_paths
from agentgit.core.research import AgentState, ModelRequest
from agentgit.core.snapshots import StateSnapshot
from agentgit.diagnosis.interventions import candidate_request
from agentgit.experiments.validation import evaluate_frozen


def frozen_fixture(protocol, output=OUTPUT):
    path = output / "frozen/tests/integration/diagnosis_dataset.py"
    assert sha(path) == protocol["files"]["tests/integration/diagnosis_dataset.py"]
    name = "frozen_diagnosis_effect_fixture"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def audit_case(folder, *, protocol_loader=load_protocol, output=OUTPUT):
    protocol, identity = protocol_loader()
    result_path, bundle_path = folder / "result.json", folder / "evidence.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert result["protocol_ref"] == identity and not (folder / "driver-failure.json").exists()
    assert result["source_before"] == result["source_after"] and result["static_report_unchanged"]
    verify_repository_evidence(bundle, result["source"])
    contents = {
        ref: base64.b64decode(item["base64"], validate=True)
        for ref, item in bundle["objects"].items()
    }

    def body(ref, kind=ObjectType.BLOB):
        assert bundle["objects"][ref]["object_type"] == kind.value
        return json.loads(contents[ref])

    class Archive:
        def get(self, ref, kind):
            assert bundle["objects"][ref]["object_type"] == kind.value
            return contents[ref]

    fixture = frozen_fixture(protocol, output)
    case = next(item for item in fixture.load_dataset().cases if item.id == result["case_id"])
    condition = body(result["condition_ref"])
    assert condition == dict(
        protocol_ref=identity,
        condition=dict(operation=case.operation, target=case.target),
        enabled_in_both_groups=True,
    )
    reference = result["source"]["reference_run_id"]
    usage = {}
    for call in bundle["indexes"]["external_calls"]:
        intent = body(call["intent_ref"], ObjectType.CALL)
        saved = body(call["result_ref"], ObjectType.CALL)
        assert intent["request_ref"] == call["request_digest"]
        assert intent["occurrence"]["provider"] == call["provider"]
        assert intent["effect"] == call["effect"]
        assert saved["complete"]
        request = contents[intent["request_ref"]]
        faults = fixture.Faults(case, call["run_id"] != reference)
        is_model = call["provider"].startswith("llm:")
        error_code = None
        try:
            if is_model:
                assert call["provider"] == "llm:mock-research/v1" and call["effect"] == "READ_ONLY"
                parsed = parse_contract(request, ModelRequest)
                assert parsed.state.goal.text == "比较三个 fixture 框架"
                assert parsed.available_tools == ("corpus_search", "source_read", "artifact_write")
                assert call["signature_digest"] == fixture.FixtureModel.signature
                expected = faults.model(request)
            else:
                name = call["provider"].split(":", 1)[1].split("/", 1)[0]
                assert name in ("corpus_search", "source_read", "artifact_write")
                assert call["effect"] == (
                    "IDEMPOTENT_WRITE" if name == "artifact_write" else "READ_ONLY"
                )
                corpus = faults.corpus()

                def original(tool, arguments, corpus=corpus):
                    if tool == "corpus_search":
                        return encode(
                            dict(
                                schema_version=1,
                                source_ids=list(corpus.search(tuple(arguments["frameworks"]))),
                            )
                        )
                    if tool == "source_read":
                        return encode(
                            dict(
                                schema_version=1,
                                source=corpus.read(arguments["source_id"]).model_dump(mode="json"),
                            )
                        )
                    validate_artifact_paths([arguments["path"]])
                    return encode(
                        dict(
                            schema_version=1,
                            path=arguments["path"],
                            content_ref=object_digest(ObjectType.BLOB, arguments["text"].encode()),
                        )
                    )

                session = SimpleNamespace(
                    tools=SimpleNamespace(invoke=original),
                    state=SimpleNamespace(
                        pending_tool=SimpleNamespace(node_key=call["logical_step_id"])
                    ),
                )
                faults.tools(session)
                expected = session.tools.invoke(name, json.loads(request))
        except AgentGitError as error:
            error_code = error.code.value
            expected = b""
        assert (saved["error"]["code"] if saved["error"] else None) == error_code
        assert contents[saved["response_ref"]] == expected and saved["response_bytes"] == len(
            expected
        )
        if call["origin_call_id"] is None:
            counter = usage.setdefault(call["run_id"], Counter())
            counter["model_calls" if is_model else "tool_calls"] += 1
            if is_model:
                counter["mock_utf8_bytes"] += len(request) + len(expected)
    static = result["report"]["report"]["diagnosis"]
    assert static["rules_digest"] == protocol["rules_digest"]
    assert static["mode"] == "static_only" and static["intervention_status"] == "not_run"
    assert static["intervention_improvement"] is None
    effect = result["effect"]
    counts = {}
    attempts = result["context_attempts"]
    candidates = static["candidates"]
    assert [a["event_id"] for a in attempts] == [
        c["event"]["event_id"] for c in candidates[: len(attempts)]
    ]
    if effect is None:
        assert result["effect_status"] == "static_only"
        assert all(
            job["run_id"] in (reference, result["source"]["failed_run_id"])
            for job in bundle["indexes"]["jobs"]
        )
        assert all(item["error"] is not None for item in attempts)
    else:
        bound = result["bound"]
        context = bound["context"]
        assert attempts[-1]["error"] is None and all(a["error"] is not None for a in attempts[:-1])
        candidate, requested = candidate_request(
            parse_contract(encode(result["report"]["report"]), DebugReport),
            attempts[-1]["event_id"],
        )
        assert candidate.model_dump(mode="json") == bound["candidate"]
        assert requested.event.model_dump(mode="json") == bound["request_event"]
        assert context["target_event_id"] == requested.event.event_id
        call = next(
            c for c in bundle["indexes"]["external_calls"] if c["id"] == context["source_call_id"]
        )
        source_intent = body(call["intent_ref"], ObjectType.CALL)
        assert context["base_commit"] == source_intent["checkpoint"]
        snapshot = body(context["precondition"]["state_ref"], ObjectType.STATE)
        payload = fixed_payload(context, snapshot)
        payload_type = PlanPatch if payload["kind"] == "PLAN_PATCH" else PromptPatch
        assert result["patch"]["payload"] == parse_contract(
            encode(dict(schema_version=1, **payload)), payload_type
        ).model_dump(mode="json")
        experiment = effect["experiment"]
        request = experiment["manifest"]["request"]
        assert experiment["manifest"]["mode"] == "live"
        assert request["budget"] == protocol["trial_budget"] and request["repeats"] == 5
        assert (
            not request["groups"][0].get("interventions")
            and len(request["groups"][1]["interventions"]) == 1
        )
        assert request["source_run_id"] == result["source"]["failed_run_id"]
        assert request["from_commit"] == context["base_commit"]
        assert len(experiment["trials"]) == len({t["job_id"] for t in experiment["trials"]}) == 10
        groups = []
        for key in ("A", "B"):
            selected = [t for t in experiment["trials"] if t["group"] == key]
            assert len(selected) == 5
            group = dict(key=key, total=5, passed=0, incomplete=0)
            for trial in selected:
                assert trial["status"] in (
                    "completed",
                    "failed",
                    "budget_exhausted",
                    "cancelled",
                    "needs_attention",
                )
                amount = usage.get(trial["run_id"], {})
                actual = trial["replay"]["actual"]
                assert actual["model_calls"] == amount.get("model_calls", 0)
                assert actual["tool_calls"] == amount.get("tool_calls", 0)
                assert actual["model_usage"]["mock"]["total_bytes"] == amount.get(
                    "mock_utf8_bytes", 0
                )
                assert amount.get("model_calls", 0) <= protocol["trial_budget"]["max_model_calls"]
                assert amount.get("tool_calls", 0) <= protocol["trial_budget"]["max_tool_calls"]
                assert amount.get("mock_utf8_bytes", 0) <= protocol["trial_budget"]["max_tokens"]
                assessment = trial["assessment"]
                if assessment is not None:
                    snapshot = parse_contract(contents[assessment["state_ref"]], StateSnapshot)
                    state = parse_contract(encode(snapshot.data), AgentState)
                    validator = parse_contract(
                        contents[assessment["validator_ref"]], ValidatorManifest
                    )
                    assert assessment["validator_ref"] == request["validator_ref"]
                    verdict, evaluation = evaluate_frozen(
                        Archive(),
                        validator,
                        state,
                        {file.path: contents[file.object_ref] for file in snapshot.artifacts},
                    )
                    assert verdict == assessment["verdict"] == trial["verdict"]
                    assert evaluation.model_dump(mode="json") == assessment["evaluation"]
                else:
                    assert trial["verdict"] == "incomplete"
                group["passed"] += trial["verdict"] == "passed"
                group["incomplete"] += trial["verdict"] == "incomplete"
            groups.append(group)
        assert groups == experiment["groups"]
        complete = not any(group["incomplete"] for group in groups)
        assert (
            result["effect_status"]
            == effect["effect_status"]
            == ("assessed" if complete else "incomplete")
        )
        delta = effect["success_rate_difference"]
        if complete:
            fraction = Fraction(groups[1]["passed"] - groups[0]["passed"], 5)
            assert delta == dict(
                numerator=str(fraction.numerator), denominator=str(fraction.denominator)
            )
        else:
            assert delta is None
        counts = dict(groups=groups, delta=delta)
    all_physical = {
        key: sum(v.get(key, 0) for v in usage.values())
        for key in ("model_calls", "tool_calls", "mock_utf8_bytes")
    }
    return dict(
        case_id=result["case_id"],
        effect_status=result["effect_status"],
        counts=counts,
        all_physical=all_physical,
        objects=len(contents),
        raw_bytes=bundle["raw_bytes"],
        result_sha256=sha(result_path),
        evidence_sha256=sha(bundle_path),
        protocol_ref=identity,
        verifier_sha256=sha(__file__),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=type(OUTPUT), required=True)
    args = parser.parse_args()
    protocol, identity = load_protocol()
    receipts = [audit_case(OUTPUT / "cases" / case_id) for case_id in protocol["cases"]]
    assert len(receipts) == 20 and len({r["case_id"] for r in receipts}) == 20
    physical = {
        key: sum(r["all_physical"][key] for r in receipts)
        for key in ("model_calls", "tool_calls", "mock_utf8_bytes")
    }
    effects = Counter(r["effect_status"] for r in receipts)
    deltas = Counter(str(r["counts"].get("delta")) for r in receipts)
    value = dict(
        kind="diagnosis-candidate-effect-audit-summary/v1",
        protocol_ref=identity,
        complete=True,
        cases=receipts,
        effect_statuses=dict(effects),
        deltas=dict(deltas),
        all_physical=physical,
        objects=sum(r["objects"] for r in receipts),
        raw_bytes=sum(r["raw_bytes"] for r in receipts),
        analysis_verifier_sha256=sha(__file__),
        ranking_metrics_changed=False,
        human_review="pending_human",
        study_scope="engineering-only/previously-seen-twenty",
    )
    load_protocol()
    args.destination.mkdir(parents=True, exist_ok=False)
    (args.destination / "verified").mkdir()
    for receipt in receipts:
        with (args.destination / "verified" / (receipt["case_id"] + ".json")).open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(receipt, stream, indent=2)
    target = args.destination / "summary.json"
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2)
    print(
        json.dumps(
            dict(
                path=str(target),
                sha256=sha(target),
                effects=dict(effects),
                deltas=dict(deltas),
                all_physical=physical,
                objects=value["objects"],
                raw_bytes=value["raw_bytes"],
            )
        )
    )


if __name__ == "__main__":
    main()
