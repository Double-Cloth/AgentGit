"""离线重算批准与独立合并历史绑定，复用来源/候选的原始字节核对。"""

import argparse
import base64
import hashlib
import json
from pathlib import Path

from merge_proposal_verify import verify_pair

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.merge_proposals import (
    ApproveMergeProposal,
    MergeApproval,
    MergeProposalManifest,
    MergeReviewTicket,
    MergeRevisionBody,
    MergeValidationRecord,
)
from agentgit.core.models import CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.snapshots import EventManifest, EventPayload, StateSnapshot


def verify_approval_pair(source, final, revision, lock):
    result = verify_pair(source, final, revision, lock)
    rows = final["indexes"]
    repo = final["repository_id"]
    refs = {
        (row["owner_type"], row["owner_id"], row["role"], row["object_id"])
        for row in rows["object_refs"]
    }

    def payload(ref, kind=ObjectType.BLOB):
        item = final["objects"][ref]
        assert item["object_type"] == kind.value
        return base64.b64decode(item["base64"], validate=True)

    def typed(ref, model, kind=ObjectType.BLOB):
        return parse_contract(payload(ref, kind), model)

    def owned(ref, owner, identity, role):
        assert (owner, identity, role, ref) in refs

    roots = {row["id"]: row for row in rows["merge_proposals"]}
    revisions = {row["id"]: row for row in rows["merge_revisions"]}
    reviews = {row["id"]: row for row in rows["merge_review_sessions"]}
    validations = {row["id"]: row for row in rows["merge_validations"]}
    commits = {row["id"]: row for row in rows["commits"]}
    for approved in rows["merge_approvals"]:
        decision = typed(approved["decision_ref"], MergeApproval)
        owned(approved["decision_ref"], "merge_approval", approved["id"], "decision")
        root = roots[approved["proposal_id"]]
        proposal = typed(root["manifest_ref"], MergeProposalManifest)
        version = revisions[approved["revision_id"]]
        body = typed(version["revision_ref"], MergeRevisionBody)
        review = reviews[approved["review_id"]]
        ticket = typed(review["ticket_ref"], MergeReviewTicket)
        validation = validations[review["validation_id"]]
        checked = typed(validation["validation_ref"], MergeValidationRecord)
        assert checked.result.validated and validation["passed"]
        assert (
            review["proposal_id"] == approved["proposal_id"]
            and review["revision_id"] == approved["revision_id"]
            and validation["revision_id"] == approved["revision_id"]
            and review["actor"] == approved["actor"] == ticket.actor == decision.actor
            and decision.proposal_ref == root["manifest_ref"]
            and decision.revision_ref == version["revision_ref"]
            and decision.candidate_digest
            == body.candidate_digest
            == checked.result.candidate_digest
            and decision.ticket_ref == review["ticket_ref"]
            and decision.validation_ref == validation["validation_ref"]
            and decision.ours == proposal.request.ours
            and decision.theirs == proposal.request.theirs
            and decision.execution == "requires_new_run"
        )
        request = ApproveMergeProposal(
            expected_revision=version["number"],
            expected_revision_ref=version["revision_ref"],
            review_id=review["id"],
            ticket_ref=review["ticket_ref"],
            candidate_digest=body.candidate_digest,
        )
        assert approved["request_digest"] == object_digest(
            ObjectType.BLOB,
            encode(dict(repository_id=repo, request=request.model_dump(mode="json"))),
        )
        commit_id = decision.commit_id
        assert commit_id == approved["commit_id"]
        manifest = typed(commit_id, CommitManifest, ObjectType.COMMIT)
        indexed = commits[commit_id]
        assert manifest.repository_id == repo
        for field in ("state_ref", "environment_ref", "event_manifest_ref"):
            assert getattr(manifest, field) == indexed[field]
        parent_rows = sorted(
            (row for row in rows["commit_parents"] if row["commit_id"] == commit_id),
            key=lambda row: row["parent_order"],
        )
        assert [row["parent_order"] for row in parent_rows] == [0, 1]
        assert (
            tuple(row["parent_id"] for row in parent_rows)
            == manifest.parents
            == (
                decision.ours.head,
                decision.theirs.head,
            )
        )
        snapshot = typed(manifest.state_ref, StateSnapshot, ObjectType.STATE)
        assert body.state is not None and snapshot.data == body.state.model_dump(mode="json")
        assert snapshot.data["execution"] == "requires_new_run" and "messages" not in snapshot.data
        for document in body.artifacts:
            entries = [entry for entry in snapshot.artifacts if entry.path == document.path]
            assert len(entries) == (1 if document.text is not None else 0)
            if entries:
                assert payload(entries[0].object_ref) == document.text.encode("utf-8")
        environment = typed(manifest.environment_ref, EnvironmentManifest, ObjectType.ENVIRONMENT)
        assert (
            environment.runtime_version == "agentgit-merge/v1"
            and environment.code_revision == revision
            and environment.dependency_lock_digest == lock
            and environment.model_signature == "none:merge/v1"
            and environment.tool_signatures == ()
            and environment.evaluation_time == body.state.constraints.evaluation_time
        )
        frozen = json.loads(payload(environment.input_ref))
        assert frozen == dict(
            schema_version=1,
            kind="merged_input/v1",
            proposal_ref=root["manifest_ref"],
            revision_ref=version["revision_ref"],
            ticket_ref=review["ticket_ref"],
            validator_ref=proposal.request.validator_ref,
            execution="requires_new_run",
        )
        events = typed(manifest.event_manifest_ref, EventManifest, ObjectType.EVENT_MANIFEST)
        assert manifest.event_range.first_seq == manifest.event_range.last_seq == 1
        assert len(events.events) == 1
        event = events.events[0]
        assert event.seq == 1 and event.type == "merge.approved" and event.cause is None
        audit = typed(event.payload_ref, EventPayload, ObjectType.EVENT)
        assert audit.data == dict(
            approval_id=approved["id"],
            proposal_ref=root["manifest_ref"],
            revision_ref=version["revision_ref"],
            ticket_ref=review["ticket_ref"],
            actor=decision.actor,
            execution="requires_new_run",
            candidate_digest=body.candidate_digest,
        )
        for role, ref in (
            ("manifest", commit_id),
            ("state", manifest.state_ref),
            ("environment", manifest.environment_ref),
            ("input", environment.input_ref),
            ("events", manifest.event_manifest_ref),
            ("merge_event", event.payload_ref),
        ):
            owned(ref, "commit", commit_id, role)
        branches = {row["name"]: row for row in rows["branches"]}
        assert branches[decision.ours.name]["head_commit_id"] == commit_id
        assert branches[decision.ours.name]["version"] == decision.ours.version + 1
        assert branches[decision.theirs.name]["head_commit_id"] == decision.theirs.head
        assert branches[decision.theirs.name]["version"] == decision.theirs.version
        assert not any(row["commit_id"] == commit_id for row in rows["run_commits"])
        assert not any(row["proposal_id"] == root["id"] for row in rows["merge_rejections"])
    # 源头变化案例由夹具主动推进一条历史；其余合并不能改变执行表或旧历史行。
    changed_head_fixture = final["details"].get("result") == "stale"
    for name, original in source["indexes"].items():
        if name.startswith("merge_") or name in ("object_refs", "branches"):
            continue
        if name in ("commits", "commit_parents", "artifacts"):
            assert all(row in rows[name] for row in original)
        elif changed_head_fixture and name in ("runs", "run_commits", "snapshots", "events"):
            continue
        else:
            assert rows[name] == original, name
    result["approvals"] = len(rows["merge_approvals"])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    results = []
    for path in sorted((args.evidence / "cases").glob("*/final/evidence.json")):
        final = json.loads(path.read_text("utf-8"))
        source = json.loads((path.parent.parent / "source/evidence.json").read_text("utf-8"))
        checked = verify_approval_pair(
            source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
        )
        checked.update(
            case=path.parent.parent.name,
            evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        results.append(checked)
    assert len(results) == 9
    summary = dict(
        kind="S11-approval-raw-audit/v1",
        source_revision=frozen["source_revision"],
        results=results,
        scope="engineering-not-S11-exit",
    )
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
