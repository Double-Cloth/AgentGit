"""从归档 CAS/索引独立核对研究合并预览；不连接数据库或执行工具。"""

import argparse
import base64
import hashlib
import json
from pathlib import Path

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import ReadArguments
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.experiments import FrozenCorpus, ValidatorManifest
from agentgit.core.merge import MergedResearchState, MergeValidation
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, SavedCallOutcome
from agentgit.core.snapshots import StateSnapshot
from agentgit.runtime.validation import validate_report
from agentgit.services.environment import source_revision


def verify_case(folder: Path) -> dict:
    source = json.loads((folder / "source/evidence.json").read_text("utf-8"))
    path = folder / "validated/evidence.json"
    data = json.loads(path.read_text("utf-8"))
    assert source["objects"] == data["objects"]
    for name in source["indexes"]:
        assert sorted(map(encode, source["indexes"][name])) == sorted(
            map(encode, data["indexes"][name])
        )
    payloads = {}
    total = 0
    for ref, entry in data["objects"].items():
        payload = base64.b64decode(entry["base64"], validate=True)
        assert object_digest(ObjectType(entry["object_type"]), payload) == ref
        assert len(payload) == entry["byte_length"]
        payloads[ref] = payload
        total += len(payload)
    assert total == data["raw_bytes"]

    def get(ref, kind):
        assert data["objects"][ref]["object_type"] == kind.value
        return payloads[ref]

    rows = data["indexes"]
    parents = {}
    for item in rows["commit_parents"]:
        parents.setdefault(item["commit_id"], []).append(item["parent_id"])

    def ancestors(head):
        found = set()
        pending = [head]
        while pending:
            node = pending.pop()
            if node not in found:
                assert len(found) < 10000
                found.add(node)
                pending.extend(parents.get(node, ()))
        return found

    validation = parse_contract(encode(data["validation"]), MergeValidation)
    assert validation.code_revision == data["source_revision"] == source_revision()
    current = parse_contract(encode(data["state"]), MergedResearchState)
    validator = parse_contract(get(validation.validator_ref, ObjectType.BLOB), ValidatorManifest)
    frozen = parse_contract(get(validator.corpus_ref, ObjectType.BLOB), FrozenCorpus)
    corpus = Corpus(frozen.sources, evaluation_time=frozen.evaluation_time)
    assert corpus.digest() == validator.corpus_digest
    assert any(
        item["object_id"] == validation.validator_ref
        and item["owner_type"] == "validator"
        and item["role"] == "manifest"
        for item in rows["object_refs"]
    )
    inputs = {}
    configurations = []
    for origin, audit in zip(validation.origins, current.source_usage, strict=True):
        manifest = parse_contract(get(origin.commit_id, ObjectType.COMMIT), CommitManifest)
        snapshot = parse_contract(get(manifest.state_ref, ObjectType.STATE), StateSnapshot)
        state = parse_contract(encode(snapshot.data), AgentState)
        environment = parse_contract(
            get(manifest.environment_ref, ObjectType.ENVIRONMENT), EnvironmentManifest
        )
        controlled = decode(get(environment.input_ref, ObjectType.BLOB))
        assert controlled["corpus_digest"] == corpus.digest() and state.goal == current.goal
        assert audit.origin == origin and audit.usage == state.usage
        configurations.append(parse_contract(encode(controlled["config"]), AgentConfig))
        inputs[(origin.run_id, origin.commit_id)] = state
    assert current.constraints.limits.within(*(item.limits for item in configurations))
    assert set(current.constraints.tools) == set.intersection(
        *(set(item.tools) for item in configurations)
    )
    assert current.execution == "requires_new_run" and "messages" not in data["state"]
    calls = {item["id"]: item for item in rows["external_calls"]}
    runs = {item["id"]: item for item in rows["runs"]}
    inherited = 0
    for proof in current.read_proofs:
        origin = proof.origin
        state = inputs[(origin.run_id, origin.commit_id)]
        assert proof.evidence == state.working_memory[proof.evidence.source.id]
        assert proof.trace in state.messages and proof.trace.provider == "tool:source_read/v1"
        row = calls[proof.call_id]
        assert row["run_id"] == proof.call_run_id and row["status"] == "succeeded"
        assert row["effect"] == "READ_ONLY" and row["provider"] == proof.trace.provider
        family = set()
        run = origin.run_id
        while run is not None:
            assert run not in family and len(family) < 100
            family.add(run)
            run = runs[run]["origin_run_id"]
        assert proof.call_run_id in family
        inherited += proof.call_run_id != origin.run_id
        intent = parse_contract(get(row["intent_ref"], ObjectType.CALL), CallIntent)
        assert intent.checkpoint in ancestors(origin.commit_id)
        assert all(
            getattr(intent.occurrence, key) == row[key]
            for key in (
                "provider",
                "logical_step_id",
                "call_index",
                "attempt",
                "signature_digest",
                "request_digest",
            )
        )
        assert (proof.trace.logical_step_id, proof.trace.call_index, proof.trace.attempt) == (
            proof.evidence.logical_step_id,
            proof.evidence.call_index,
            proof.evidence.attempt,
        )
        assert proof.trace.signature_digest == row["signature_digest"]
        assert proof.trace.request_ref == intent.request_ref == row["request_digest"]
        request = parse_contract(get(intent.request_ref, ObjectType.BLOB), ReadArguments)
        assert request.source_id == proof.evidence.source.id
        saved = parse_contract(get(row["result_ref"], ObjectType.CALL), SavedCallOutcome)
        assert (
            saved.complete
            and saved.error is None
            and saved.response_ref == proof.trace.response_ref
        )
        response = decode(get(saved.response_ref, ObjectType.BLOB))
        assert response == dict(
            schema_version=1, source=proof.evidence.source.model_dump(mode="json")
        )
        assert proof.evidence.source == corpus.read(request.source_id)
    for claim in current.completed_audit:
        state = inputs[(claim.origin.run_id, claim.origin.commit_id)]
        assert claim.node.key in state.completed and set(claim.node.dependencies) <= set(
            state.completed
        )
        assert claim.node in state.plan.nodes and claim.node in current.plan.nodes
    for item in current.effects_audit:
        row = calls[item.call_id]
        assert (
            item.run_id == row["run_id"]
            and item.intent_ref == row["intent_ref"]
            and item.result_ref == row["result_ref"]
        )
        assert (
            item.status == row["status"] == "succeeded"
            and item.provider == "tool:artifact_write/v1"
        )
    contents = {
        item["path"]: item["text"].encode("utf-8")
        for item in data["artifacts"]
        if item["text"] is not None
    }
    evaluation = validate_report(
        AgentState(goal=current.goal, plan=current.plan, working_memory=current.working_memory),
        contents,
        corpus,
    )
    assert evaluation == validation.report_evaluation and evaluation.passed == validation.validated
    digest = object_digest(
        ObjectType.BLOB,
        encode(
            dict(
                schema_version=1,
                origins=[item.model_dump(mode="json") for item in validation.origins],
                validator_ref=validation.validator_ref,
                source_revision=validation.code_revision,
                dependency_lock_digest=validation.dependency_lock_digest,
                state=data["state"],
                artifacts=[
                    dict(
                        path=item["path"],
                        media_type="application/json"
                        if item["path"] == "sources.json"
                        else "text/markdown",
                        status=item["status"],
                        content_ref=object_digest(ObjectType.BLOB, item["text"].encode())
                        if item["text"] is not None
                        else None,
                    )
                    for item in data["artifacts"]
                ],
            )
        ),
    )
    assert digest == validation.candidate_digest
    assert all(row["status"] in ("completed", "failed") for row in rows["runs"])
    assert len(rows["runs"]) == 3 and len(rows["jobs"]) == 2
    assert all(
        row["lease_until"] is None and row["status"] in ("completed", "failed")
        for row in rows["jobs"]
    )
    sql = data["observed_sql"]
    assert sql and any(item == "SET TRANSACTION READ ONLY" for item in sql)
    assert all(branch["active_writer_run_id"] is None for branch in data["branches"].values())
    actual_models = 0
    actual_tools = 0
    actual_bytes = 0
    for row in calls.values():
        assert row["result_ref"] is not None and row["status"] == "succeeded"
        if row["origin_call_id"] is None:
            if row["provider"].startswith("llm:"):
                actual_models += 1
                saved = parse_contract(get(row["result_ref"], ObjectType.CALL), SavedCallOutcome)
                actual_bytes += (
                    len(get(row["request_digest"], ObjectType.BLOB)) + saved.response_bytes
                )
            elif row["provider"].startswith("tool:"):
                actual_tools += 1
    return dict(
        case=folder.name,
        validated=validation.validated,
        read_proofs=len(current.read_proofs),
        inherited_proofs=inherited,
        models=actual_models,
        tools=actual_tools,
        mock_utf8_bytes=actual_bytes,
        objects=len(payloads),
        raw_bytes=total,
        evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    snapshot = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    assert snapshot["source_revision"] == source_revision()
    results = [
        verify_case(args.evidence / "cases" / name) for name in ("fresh-branches", "invalid-claim")
    ]
    assert [item["validated"] for item in results] == [True, False]
    summary = dict(
        kind="S11-research-preview-audit/v1",
        source_revision=source_revision(),
        results=results,
        scope="engineering-not-S11-exit",
    )
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
