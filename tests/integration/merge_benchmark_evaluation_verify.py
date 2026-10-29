"""独立核对三方法原始候选、内容预期、批准与真实后续执行；不连接数据库。"""

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path

from merge_benchmark_verify import verify as verify_source
from merge_proposal_verify import verify_pair
from merge_run_verify import verify_run_pair

from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError
from agentgit.core.merge import MergeValidation
from agentgit.core.merge_proposals import MergeRevisionBody
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.snapshots import ArtifactEntry
from agentgit.merge.artifacts import ArtifactVersion, merge_artifact

METHODS = ("semantic", "artifact_only", "choose_ours")


def inspect_case(source, middle, final, frozen, spec, stage):
    prepared = verify_source(source, middle, frozen)
    assert final["source_revision"] == frozen["source_revision"]
    assert source["repository_id"] == final["repository_id"]
    assert middle["details"]["stage"] == final["details"]["stage"] == stage
    assert source["details"]["case"] == spec
    assert middle["details"]["methods"] == final["details"]["methods"]
    rows = source["indexes"]
    for ref, item in final["objects"].items():
        data = base64.b64decode(item["base64"], validate=True)
        assert object_digest(ObjectType(item["object_type"]), data) == ref
        assert len(data) == item["byte_length"]
    assert sum(item["byte_length"] for item in final["objects"].values()) == final["raw_bytes"]
    assert all(final["objects"][ref] == item for ref, item in source["objects"].items())
    assert all(
        row["repository_id"] == final["repository_id"]
        for items in final["indexes"].values()
        for row in items
    )

    def read(ref, kind=ObjectType.BLOB):
        item = final["objects"][ref]
        assert item["object_type"] == kind.value
        return base64.b64decode(item["base64"], validate=True)

    def typed(ref, model, kind=ObjectType.BLOB):
        return parse_contract(read(ref, kind), model)

    pins = final["details"]["proposal"]
    heads = (source["details"]["base"], pins["ours"]["head"], pins["theirs"]["head"])
    commits = [typed(head, CommitManifest, ObjectType.COMMIT) for head in heads]
    snapshots = [decode(read(commit.state_ref, ObjectType.STATE)) for commit in commits]
    states = [snapshot["data"] for snapshot in snapshots]
    configs = [
        decode(
            read(
                typed(commit.environment_ref, EnvironmentManifest, ObjectType.ENVIRONMENT).input_ref
            )
        )["config"]
        for commit in commits
    ]
    parents = {}
    for item in rows["commit_parents"]:
        parents.setdefault(item["commit_id"], set()).add(item["parent_id"])

    def ancestors(head):
        found, pending = set(), [head]
        while pending:
            item = pending.pop()
            if item not in found:
                assert len(found) < 10000
                found.add(item)
                pending.extend(parents.get(item, ()))
        return found

    common = ancestors(heads[1]) & ancestors(heads[2])
    assert common - {p for node in common for p in parents.get(node, ())} == {heads[0]}
    by_run = {row["id"]: row for row in rows["runs"]}

    def readable_calls(run_id, state, head):
        family = set()
        while run_id is not None:
            assert run_id not in family and len(family) < 100
            family.add(run_id)
            run_id = by_run[run_id]["origin_run_id"]
        allowed = set()
        history = ancestors(head)
        for call in rows["external_calls"]:
            if (
                call["provider"] != "tool:source_read/v1"
                or call["status"] != "succeeded"
                or call["run_id"] not in family
            ):
                continue
            intent = typed(call["intent_ref"], CallIntent, ObjectType.CALL)
            request = decode(read(intent.request_ref))
            evidence = state["working_memory"].get(request["source_id"])
            if (
                evidence is not None
                and intent.checkpoint in history
                and evidence["logical_step_id"] == call["logical_step_id"]
                and evidence["call_index"] == call["call_index"]
                and evidence["attempt"] == call["attempt"]
            ):
                allowed.add(call["id"])
        return allowed

    artifact_tables = [
        {item["path"]: item for item in snapshot["artifacts"]} for snapshot in snapshots
    ]
    expected_artifacts, artifact_conflicts, artifact_error = [], [], None
    for name in ("report.md", "sources.json"):
        versions = []
        for table in artifact_tables:
            item = table.get(name)
            entry = parse_contract(encode(item), ArtifactEntry) if item else None
            versions.append(ArtifactVersion(entry, read(item["object_ref"])) if entry else None)
        try:
            artifact = merge_artifact(name, *versions)
        except AgentGitError as error:
            artifact_error = error.as_dict()
            break
        expected_artifacts.append(
            dict(
                path=artifact.path,
                media_type=artifact.media_type,
                status=artifact.status,
                text=artifact.content.decode() if artifact.content is not None else None,
                evidence_refs=list(artifact.evidence_refs),
            )
        )
        artifact_conflicts.extend(item.model_dump(mode="json") for item in artifact.conflicts)

    records = final["details"]["methods"]
    assert [item["method"] for item in records] == list(METHODS)
    ours = records[2]
    assert ours["state"] == states[1] and ours["config"] == configs[1]
    assert ours["decision"] == "candidate" and not ours["publishable"]
    assert ours["read_call_ids"] == sorted(
        readable_calls(pins["ours"]["source_run_id"], states[1], heads[1])
    )
    assert ours["artifacts"] == [
        dict(
            path=item["path"],
            media_type=item["media_type"],
            status="merged",
            text=read(item["object_ref"]).decode(),
            evidence_refs=[item["object_ref"]],
        )
        for item in snapshots[1]["artifacts"]
    ]
    baseline = records[1]
    assert baseline["state"] is None and not baseline["publishable"]
    assert (
        baseline["artifacts"] == expected_artifacts and baseline["conflicts"] == artifact_conflicts
    )
    assert baseline["error"] == artifact_error
    assert baseline["decision"] == (
        "input_rejected" if artifact_error else "conflict" if artifact_conflicts else "candidate"
    )
    semantic = records[0]
    assert not semantic["publishable"]
    if semantic["state"] is not None:
        proofs = semantic["state"]["read_proofs"]
        assert semantic["read_call_ids"] == sorted({item["call_id"] for item in proofs})
        for proof in proofs:
            origin = proof["origin"]
            origin_state = decode(
                read(
                    typed(origin["commit_id"], CommitManifest, ObjectType.COMMIT).state_ref,
                    ObjectType.STATE,
                )
            )["data"]
            assert proof["call_id"] in readable_calls(
                origin["run_id"], origin_state, origin["commit_id"]
            )
    else:
        assert semantic["read_call_ids"] == []
    if semantic["validation"] is not None:
        validation = parse_contract(encode(semantic["validation"]), MergeValidation)
        assert semantic["conflicts"] == [
            item.model_dump(mode="json") for item in validation.conflicts
        ]
        assert semantic["decision"] == ("candidate" if validation.validated else "conflict")
        assert semantic["artifacts"] == expected_artifacts and artifact_error is None
        calculated = object_digest(
            ObjectType.BLOB,
            encode(
                dict(
                    schema_version=1,
                    origins=[item.model_dump(mode="json") for item in validation.origins],
                    validator_ref=pins["validator_ref"],
                    source_revision=frozen["source_revision"],
                    dependency_lock_digest=frozen["files"]["uv.lock"],
                    state=semantic["state"],
                    artifacts=[
                        dict(
                            path=item["path"],
                            media_type=item["media_type"],
                            status=item["status"],
                            content_ref=object_digest(ObjectType.BLOB, item["text"].encode())
                            if item["text"] is not None
                            else None,
                        )
                        for item in semantic["artifacts"]
                    ],
                )
            ),
        )
        assert calculated == validation.candidate_digest
        assert {item.kind.value for item in validation.conflicts} >= set(spec["kinds"])
    else:
        assert semantic["decision"] == "input_rejected"
        assert semantic["error"]["code"] in spec["errors"]
    workflow = final["details"]["workflow"]
    if spec["expected"] == "accept":
        total = verify_run_pair(
            source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
        )
        assert len(final["indexes"]["merge_approvals"]) == 1
        assert workflow["evaluation"]["passed"]
        assert workflow["automated_review_tickets"] == 1
    else:
        for name, old in rows.items():
            if not name.startswith("merge_") and name != "object_refs":
                assert final["indexes"][name] == old, name
        assert not final["indexes"]["merge_approvals"]
        assert workflow["automated_review_tickets"] == 0
        if final["indexes"]["merge_proposals"]:
            total = verify_pair(
                source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
            )
            assert len(final["indexes"]["merge_rejections"]) == 1
        else:
            total = {**prepared, "models": prepared["models"], "tools": prepared["tools"]}
            assert workflow["publication_guard"]["code"] in (*spec["errors"], "version_conflict")
    assert workflow["independent_human_reviews"] is None
    revisions = final["indexes"]["merge_revisions"]
    if revisions:
        assert len(revisions) == 1
        body = typed(revisions[0]["revision_ref"], MergeRevisionBody)
        assert (body.state.model_dump(mode="json") if body.state else None) == semantic["state"]
        assert body.candidate_digest == semantic["validation"]["candidate_digest"]
        assert [
            dict(
                path=item.path,
                media_type=item.media_type,
                status=item.status,
                text=item.text,
                evidence_refs=list(item.evidence_refs),
            )
            for item in body.artifacts
        ] == semantic["artifacts"]

    # 无冲突十例只有保留/追加节点和补读；独立由原始两侧并集建立内容预期。
    golden = None
    if spec["expected"] == "accept":
        nodes = {}
        for state in states[1:]:
            for node in state["plan"]["nodes"]:
                assert node["key"] not in nodes or nodes[node["key"]] == node
                nodes[node["key"]] = node
        memory = {}
        for key, before in states[0]["working_memory"].items():
            left, right = (state["working_memory"][key] for state in states[1:])
            if left != before and right != before:
                assert left == right
            memory[key] = right if left == before else left
        base_report = decode(read(artifact_tables[0]["sources.json"]["object_ref"]))
        expected_report = {
            **base_report,
            "sources": [memory[item["source"]["id"]] for item in base_report["sources"]],
        }
        caps = {}
        for key in configs[0]["limits"]:
            if key != "schema_version":
                finite = [
                    cfg["limits"].get(key) for cfg in configs if cfg["limits"].get(key) is not None
                ]
                if finite:
                    caps[key] = min(finite)
        limits = dict(schema_version=1, **caps)
        tools = sorted(set.intersection(*(set(cfg["tools"]) for cfg in configs)))
        required_reads = readable_calls(pins["ours"]["source_run_id"], states[1], heads[1])
        required_reads |= readable_calls(pins["theirs"]["source_run_id"], states[2], heads[2])
        golden = dict(
            nodes=nodes,
            memory=memory,
            goal=states[0]["goal"],
            tools=tools,
            limits=limits,
            required_reads=required_reads,
            artifacts={
                "sources.json": encode(expected_report).decode(),
                "report.md": read(artifact_tables[0]["report.md"]["object_ref"]).decode(),
            },
        )
    assessments = {}
    for record in records:
        assert record["actual_new_calls"] == 0 and record["elapsed_ns"] >= 0
        decision = record["decision"]
        rejected = decision == "input_rejected"
        incomplete = rejected and record["error"]["code"] not in spec["errors"]
        candidate = decision == "candidate"
        negative = spec["expected"] == "block"
        artifacts_ok = content_ok = False
        if golden is not None:
            texts = {
                item["path"]: item["text"]
                for item in record["artifacts"]
                if item["status"] == "merged"
            }
            artifacts_ok = texts == golden["artifacts"]
            state = record["state"]
            if state is not None:
                constraints = state.get("constraints") or record["config"]
                content_ok = bool(
                    candidate
                    and artifacts_ok
                    and state["goal"] == golden["goal"]
                    and {item["key"]: item for item in state["plan"]["nodes"]} == golden["nodes"]
                    and state["working_memory"] == golden["memory"]
                    and sorted(constraints["tools"]) == golden["tools"]
                    and constraints["limits"] == golden["limits"]
                    and golden["required_reads"] <= set(record["read_call_ids"])
                )
        checked = dict(
            expected=spec["expected"],
            candidate=candidate,
            safe_rejection=negative and not candidate and not incomplete,
            conflict_detected=negative and decision == "conflict",
            input_rejected=rejected,
            incomplete=incomplete,
            false_accept=negative and candidate,
            plan_constraint_unsafe_candidate=negative and candidate and spec["group"] == "plan",
            effect_unsafe_candidate=negative and candidate and spec["id"] == "M30",
            clean_artifacts_correct=artifacts_ok,
            clean_content_correct=content_ok,
            matches_expectation=(negative and not candidate and not incomplete)
            if negative
            else content_ok,
        )
        assert checked == record["assessment"], (spec["id"], record["method"])
        assessments[record["method"]] = checked
    return dict(
        case=spec["id"],
        group=spec["group"],
        assessments=assessments,
        prepared=prepared,
        total={key: total[key] for key in ("models", "tools", "mock_utf8_bytes")},
        approved=int(spec["expected"] == "accept"),
        automated_review_tickets=workflow["automated_review_tickets"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    frozen = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    assets = json.loads(
        (args.evidence / "frozen/tests/fixtures/merge/v1/cases.json").read_text("utf-8")
    )
    specs = {item["id"]: item for item in assets["cases"]}
    expected = ["M01", "M09", "M14", "M20", "M30"] if args.pilot else sorted(specs)
    results = []
    first = None
    for name in expected:
        folder = args.evidence / "cases" / name
        source, middle, final = [
            json.loads((folder / phase / "evidence.json").read_text("utf-8"))
            for phase in ("source", "methods", "final")
        ]
        result = inspect_case(
            source, middle, final, frozen, specs[name], "pilot" if args.pilot else "formal"
        )
        if first is None:
            first = (source, middle, final, specs[name])
        result["final_sha256"] = hashlib.sha256(
            (folder / "final/evidence.json").read_bytes()
        ).hexdigest()
        results.append(result)
    negatives = []
    source, middle, final, spec = first
    for name in (
        "candidate_bytes",
        "semantic_proof_ids",
        "baseline_borrowed_proof",
        "approval_commit",
    ):
        bad_middle, bad_final = copy.deepcopy(middle), copy.deepcopy(final)
        records = bad_final["details"]["methods"]
        if name == "candidate_bytes":
            records[0]["artifacts"][0]["text"] += "\n"
        elif name == "semantic_proof_ids":
            records[0]["read_call_ids"] = records[0]["read_call_ids"][:-1]
        elif name == "baseline_borrowed_proof":
            extra = next(
                value
                for value in records[0]["read_call_ids"]
                if value not in records[2]["read_call_ids"]
            )
            records[2]["read_call_ids"] = sorted([*records[2]["read_call_ids"], extra])
        else:
            bad_final["indexes"]["merge_approvals"][0]["commit_id"] = "0" * 64
        bad_middle["details"]["methods"] = copy.deepcopy(records)
        try:
            inspect_case(
                source, bad_middle, bad_final, frozen, spec, "pilot" if args.pilot else "formal"
            )
        except AssertionError:
            negatives.append(dict(check=name, rejected=True))
        else:
            raise AssertionError("篡改副本未被拒绝：" + name)
    summary = dict(
        kind="S11-merge-evaluation-raw-audit/v1",
        results=results,
        negative_checks=negatives,
        source_revision=frozen["source_revision"],
        stage="pilot" if args.pilot else "formal",
        oracle_review="design-derived-awaiting-independent-review",
        scope="engineering-not-S11-exit",
    )
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(dict(cases=len(results), approved=sum(item["approved"] for item in results))))


if __name__ == "__main__":
    main()
