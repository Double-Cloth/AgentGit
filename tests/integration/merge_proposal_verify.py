"""独立核对耐久合并的 CAS、修订链、验证、票据与拒绝；不连接数据库。"""

import argparse
import base64
import hashlib
import json
from pathlib import Path

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.experiments import FrozenCorpus, ValidatorManifest
from agentgit.core.merge import ApprovedMergeOrigin, MergedResearchState
from agentgit.core.merge_proposals import (
    MergeApproval,
    MergeProposalManifest,
    MergeRejection,
    MergeReviewTicket,
    MergeRevisionBody,
    MergeValidationRecord,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, SavedCallOutcome
from agentgit.core.snapshots import StateSnapshot
from agentgit.runtime.validation import validate_report

MODELS = {
    "merge_proposals": ("manifest_ref", "merge_proposal", "manifest", MergeProposalManifest),
    "merge_revisions": ("revision_ref", "merge_revision", "revision", MergeRevisionBody),
    "merge_validations": (
        "validation_ref",
        "merge_validation",
        "validation",
        MergeValidationRecord,
    ),
    "merge_review_sessions": ("ticket_ref", "merge_review", "ticket", MergeReviewTicket),
    "merge_rejections": ("decision_ref", "merge_rejection", "decision", MergeRejection),
}


def verify_pair(source, final, revision, lock, *, additional_execution=False):
    assert source["repository_id"] == final["repository_id"]
    assert source["source_revision"] == final["source_revision"] == revision
    repo = final["repository_id"]
    rows = final["indexes"]
    payloads = {}
    total = 0
    for ref, item in final["objects"].items():
        payload = base64.b64decode(item["base64"], validate=True)
        assert (
            object_digest(ObjectType(item["object_type"]), payload) == ref
            and len(payload) == item["byte_length"]
        )
        payloads[ref] = payload
        total += len(payload)
    assert total == final["raw_bytes"]
    for rowset in rows.values():
        assert all(row["repository_id"] == repo for row in rowset)

    def get(ref, kind=ObjectType.BLOB):
        assert final["objects"][ref]["object_type"] == kind.value
        return payloads[ref]

    typed = {}
    refs = {
        (item["owner_type"], item["owner_id"], item["role"], item["object_id"])
        for item in rows["object_refs"]
    }
    for name, (field, owner, role, model) in MODELS.items():
        assert len(
            {(item["actor"], item["route"], item["request_key"]) for item in rows[name]}
        ) == len(rows[name])
        typed[name] = {}
        for row in rows[name]:
            assert (owner, row["id"], role, row[field]) in refs
            typed[name][row["id"]] = parse_contract(get(row[field]), model)
    roots = {item["id"]: item for item in rows["merge_proposals"]}
    revisions = {item["id"]: item for item in rows["merge_revisions"]}
    validations = {item["id"]: item for item in rows["merge_validations"]}
    calls = {item["id"]: item for item in rows["external_calls"]}
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

    def identity(request):
        return object_digest(
            ObjectType.BLOB,
            encode(dict(repository_id=repo, request=request.model_dump(mode="json"))),
        )

    def run_state(origin):
        run = next(item for item in rows["runs"] if item["id"] == origin.run_id)
        assert origin.commit_id == run["base_commit_id"] or any(
            item["run_id"] == origin.run_id and item["commit_id"] == origin.commit_id
            for item in rows["run_commits"]
        )
        commit = parse_contract(get(origin.commit_id, ObjectType.COMMIT), CommitManifest)
        snapshot = parse_contract(get(commit.state_ref, ObjectType.STATE), StateSnapshot)
        return parse_contract(encode(snapshot.data), AgentState)

    for identifier, row in roots.items():
        manifest = typed["merge_proposals"][identifier]
        common = ancestors(manifest.request.ours.head) & ancestors(manifest.request.theirs.head)
        dominated = {parent for node in common for parent in parents.get(node, ())}
        best = common - dominated
        assert manifest.origins[0].commit_id in best
        assert len(best) == 1 or manifest.request.selected_base in best
        assert row["request_digest"] == identity(manifest.request)
        assert (
            row["base_commit_id"] == manifest.origins[0].commit_id
            and row["validator_ref"] == manifest.request.validator_ref
        )
        for side in ("ours", "theirs"):
            pin = getattr(manifest.request, side)
            assert (
                row[side + "_branch"],
                row[side + "_run_id"],
                row[side + "_head"],
                row[side + "_version"],
            ) == (pin.name, pin.source_run_id, pin.head, pin.version)
        ordered = sorted(
            (item for item in revisions.values() if item["proposal_id"] == identifier),
            key=lambda item: item["number"],
        )
        assert [item["number"] for item in ordered] == list(range(1, len(ordered) + 1))
        validator = parse_contract(get(row["validator_ref"]), ValidatorManifest)
        assert validator.code_revision == revision and validator.dependency_lock_digest == lock
        frozen = parse_contract(get(validator.corpus_ref), FrozenCorpus)
        corpus = Corpus(frozen.sources, evaluation_time=frozen.evaluation_time)
        assert corpus.digest() == validator.corpus_digest
        inputs = {}
        configs = []
        for origin in manifest.origins:
            commit = parse_contract(get(origin.commit_id, ObjectType.COMMIT), CommitManifest)
            assert commit.repository_id == repo
            snapshot = parse_contract(get(commit.state_ref, ObjectType.STATE), StateSnapshot)
            if isinstance(origin, ApprovedMergeOrigin):
                approved = next(
                    item for item in rows["merge_approvals"] if item["id"] == origin.approval_id
                )
                assert approved["commit_id"] == origin.commit_id
                assert (
                    "merge_approval",
                    approved["id"],
                    "decision",
                    approved["decision_ref"],
                ) in refs
                decision = parse_contract(get(approved["decision_ref"]), MergeApproval)
                assert decision.commit_id == origin.commit_id
                merged = parse_contract(encode(snapshot.data), MergedResearchState)
                inputs[encode(origin.model_dump(mode="json"))] = AgentState(
                    goal=merged.goal, plan=merged.plan, working_memory=merged.working_memory
                )
                configs.append(merged.constraints)
                assert merged.constraints.evaluation_time == corpus.evaluation_time
                continue
            inputs[encode(origin.model_dump(mode="json"))] = run_state(origin)
            environment = parse_contract(
                get(commit.environment_ref, ObjectType.ENVIRONMENT), EnvironmentManifest
            )
            controlled = decode(get(environment.input_ref))
            configs.append(parse_contract(encode(controlled["config"]), AgentConfig))
            assert (
                controlled["corpus_digest"] == corpus.digest()
                and environment.evaluation_time == corpus.evaluation_time
            )
        for index, item in enumerate(ordered):
            body = typed["merge_revisions"][item["id"]]
            assert (
                body.revision == item["number"]
                and body.previous_ref == item["previous_ref"]
                and body.proposal_ref == row["manifest_ref"]
            )
            assert body.previous_ref == (ordered[index - 1]["revision_ref"] if index else None)
            request = (
                manifest.request
                if not index
                else ReplaceMergeRevision(
                    expected_revision=index,
                    expected_revision_ref=body.previous_ref,
                    edits=body.edits,
                )
            )
            assert item["request_digest"] == identity(request)
            assert (
                body.code_revision == revision and body.candidate_digest == item["candidate_digest"]
            )
            state = body.state
            if state is not None:
                assert (
                    state.execution == "requires_new_run" and "messages" not in state.model_dump()
                )
                assert state.constraints.limits.within(*(cfg.limits for cfg in configs))
                assert set(state.constraints.tools) == set.intersection(
                    *(set(cfg.tools) for cfg in configs)
                )
                assert [entry.origin for entry in state.source_usage] == list(manifest.origins)
                for usage in state.source_usage:
                    if isinstance(usage.origin, ApprovedMergeOrigin):
                        assert usage.usage is None and usage.execution == "not_executed"
                    else:
                        assert (
                            usage.usage
                            == inputs[encode(usage.origin.model_dump(mode="json"))].usage
                        )
                for proof in state.read_proofs:
                    origin = proof.origin
                    original = run_state(origin)
                    assert proof.evidence == original.working_memory[proof.evidence.source.id]
                    assert (
                        proof.evidence.source == corpus.read(proof.evidence.source.id)
                        and proof.trace in original.messages
                    )
                    call = calls[proof.call_id]
                    assert (
                        call["run_id"] == proof.call_run_id
                        and call["status"] == "succeeded"
                        and call["provider"] == "tool:source_read/v1"
                    )
                    intent = parse_contract(get(call["intent_ref"], ObjectType.CALL), CallIntent)
                    assert intent.checkpoint in ancestors(origin.commit_id)
                    assert all(
                        getattr(intent.occurrence, key) == call[key]
                        for key in (
                            "logical_step_id",
                            "call_index",
                            "attempt",
                            "provider",
                            "request_digest",
                            "signature_digest",
                        )
                    )
                    assert (
                        proof.trace.request_ref == call["request_digest"]
                        and proof.trace.signature_digest == call["signature_digest"]
                    )
                    saved = parse_contract(
                        get(call["result_ref"], ObjectType.CALL), SavedCallOutcome
                    )
                    assert (
                        saved.complete
                        and saved.error is None
                        and saved.response_ref == proof.trace.response_ref
                    )
                    assert decode(get(saved.response_ref)) == dict(
                        schema_version=1, source=proof.evidence.source.model_dump(mode="json")
                    )
                contents = {
                    artifact.path: artifact.text.encode()
                    for artifact in body.artifacts
                    if artifact.text is not None
                }
                evaluation = validate_report(
                    AgentState(
                        goal=state.goal, plan=state.plan, working_memory=state.working_memory
                    ),
                    contents,
                    corpus,
                )
                if not body.conflicts:
                    assert evaluation.passed
            calculated = object_digest(
                ObjectType.BLOB,
                encode(
                    dict(
                        schema_version=1,
                        origins=[origin.model_dump(mode="json") for origin in manifest.origins],
                        validator_ref=manifest.request.validator_ref,
                        source_revision=revision,
                        dependency_lock_digest=lock,
                        state=state.model_dump(mode="json") if state else None,
                        artifacts=[
                            dict(
                                path=artifact.path,
                                media_type=artifact.media_type,
                                status=artifact.status,
                                content_ref=object_digest(ObjectType.BLOB, artifact.text.encode())
                                if artifact.text is not None
                                else None,
                            )
                            for artifact in body.artifacts
                        ],
                    )
                ),
            )
            assert calculated == body.candidate_digest
    for identifier, row in validations.items():
        record = typed["merge_validations"][identifier]
        linked = revisions[row["revision_id"]]
        body = typed["merge_revisions"][row["revision_id"]]
        root = roots[row["proposal_id"]]
        manifest = typed["merge_proposals"][row["proposal_id"]]
        assert (
            record.proposal_ref == root["manifest_ref"]
            and record.revision_ref == linked["revision_ref"]
            and record.result.origins == manifest.origins
        )
        assert (
            record.result.candidate_digest == body.candidate_digest == row["candidate_digest"]
            and record.result.validated == row["passed"]
        )
        assert row["request_digest"] == identity(
            ValidateMergeRevision(
                expected_revision=linked["number"], expected_revision_ref=linked["revision_ref"]
            )
        )
        indexed = sorted(
            (entry for entry in rows["merge_conflicts"] if entry["validation_id"] == identifier),
            key=lambda entry: entry["ordinal"],
        )
        assert len(indexed) == len(record.result.conflicts)
        for position, entry in enumerate(indexed):
            conflict = record.result.conflicts[position]
            assert (
                entry["ordinal"] == position
                and entry["kind"] == conflict.kind.value
                and get(entry["conflict_ref"]) == encode(conflict.model_dump(mode="json"))
            )
            assert ("merge_conflict", entry["id"], "conflict", entry["conflict_ref"]) in refs
    for row in rows["merge_review_sessions"]:
        ticket = typed["merge_review_sessions"][row["id"]]
        linked = revisions[row["revision_id"]]
        checked = validations[row["validation_id"]]
        manifest = typed["merge_proposals"][row["proposal_id"]]
        assert (
            ticket.actor == row["actor"]
            and ticket.revision_ref == linked["revision_ref"]
            and ticket.candidate_digest == linked["candidate_digest"]
        )
        assert (
            ticket.proposal_ref == roots[row["proposal_id"]]["manifest_ref"]
            and checked["passed"]
            and checked["revision_id"] == row["revision_id"]
        )
        assert (
            ticket.validation_id == row["validation_id"]
            and ticket.validation_ref == checked["validation_ref"]
        )
        assert (
            ticket.ours == manifest.request.ours
            and ticket.theirs == manifest.request.theirs
            and ticket.execution == "not_started"
        )
        assert row["request_digest"] == identity(
            PrepareMergeReview(
                expected_revision=linked["number"],
                expected_revision_ref=linked["revision_ref"],
                validation_id=ticket.validation_id,
            )
        )
    for row in rows["merge_rejections"]:
        decision = typed["merge_rejections"][row["id"]]
        linked = revisions[row["revision_id"]]
        assert (
            decision.proposal_ref == roots[row["proposal_id"]]["manifest_ref"]
            and decision.revision_ref == linked["revision_ref"]
            and decision.actor == row["actor"]
        )
        assert decision.decision == "reject" and decision.execution == "not_started"
        assert row["request_digest"] == identity(
            RejectMergeProposal(
                expected_revision=linked["number"],
                expected_revision_ref=linked["revision_ref"],
                reason=decision.reason,
            )
        )
    for name in ("external_calls", "jobs"):
        if additional_execution:
            assert all(row in rows[name] for row in source["indexes"][name]), name
        else:
            assert rows[name] == source["indexes"][name], name
    assert all(
        row["status"] in ("completed", "failed") and row["lease_until"] is None
        for row in rows["jobs"]
    )
    assert not any(row["active_writer_run_id"] is not None for row in rows["branches"])
    models = tools = mock_bytes = 0
    for call in calls.values():
        assert call["status"] == "succeeded" and call["origin_call_id"] is None
        if call["provider"].startswith("llm:"):
            models += 1
            saved = parse_contract(get(call["result_ref"], ObjectType.CALL), SavedCallOutcome)
            mock_bytes += len(get(call["request_digest"])) + saved.response_bytes
        else:
            tools += 1
    return dict(
        repository_id=repo,
        proposals=len(roots),
        revisions=len(revisions),
        validations=len(validations),
        conflicts=len(rows["merge_conflicts"]),
        reviews=len(rows["merge_review_sessions"]),
        rejections=len(rows["merge_rejections"]),
        models=models,
        tools=tools,
        mock_utf8_bytes=mock_bytes,
        objects=len(payloads),
        raw_bytes=total,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    snapshot = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    sources = {}
    for path in (args.evidence / "cases").glob("*/source/evidence.json"):
        data = json.loads(path.read_text("utf-8"))
        sources[data["repository_id"]] = data
    results = []
    for path in sorted((args.evidence / "cases").glob("*/final/evidence.json")):
        final = json.loads(path.read_text("utf-8"))
        result = verify_pair(
            sources[final["repository_id"]],
            final,
            snapshot["source_revision"],
            snapshot["files"]["uv.lock"],
        )
        result.update(
            case=path.parent.parent.name,
            evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        results.append(result)
    assert len(results) == 6
    summary = dict(
        kind="S11-proposal-raw-audit/v1",
        source_revision=snapshot["source_revision"],
        results=results,
        scope="engineering-not-S11-exit",
    )
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
