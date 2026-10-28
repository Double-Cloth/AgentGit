"""离线核对合并来源、独立初始化、新调用、Recorded 路径及实际进程终止证据。"""

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path

from merge_approval_verify import verify_approval_pair

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.experiments import FrozenCorpus, ValidatorManifest
from agentgit.core.jobs import JobPayload
from agentgit.core.merge_runs import ForkMergedBranch, NewMergedRun
from agentgit.core.models import CallIntent, CommitManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage, SavedCallOutcome, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.merge.initialize import initialize_merged
from agentgit.storage.roles import path_reference_role


def verify_run_pair(source, final, revision, lock):
    rows = final["indexes"]
    # 原执行记录不能被新任务改写；让既有审计器只审其所属执行集合，新增执行另行核对。
    projection = copy.deepcopy(final)
    for name, original in source["indexes"].items():
        if name.startswith("merge_") or name in (
            "object_refs",
            "branches",
            "commits",
            "commit_parents",
            "artifacts",
        ):
            continue
        assert all(row in rows[name] for row in original), name
        projection["indexes"][name] = original
    summary = verify_approval_pair(source, projection, revision, lock)
    refs = {
        (item["owner_type"], item["owner_id"], item["role"], item["object_id"])
        for item in rows["object_refs"]
    }
    repo = final["repository_id"]

    def read(ref, kind=ObjectType.BLOB):
        item = final["objects"][ref]
        assert item["object_type"] == kind.value
        return base64.b64decode(item["base64"], validate=True)

    def typed(ref, model, kind=ObjectType.BLOB):
        return parse_contract(read(ref, kind), model)

    def owned(ref, owner, identity, role):
        assert (owner, identity, role, ref) in refs

    def snapshot(commit):
        manifest = typed(commit, CommitManifest, ObjectType.COMMIT)
        assert manifest.repository_id == repo
        return typed(manifest.state_ref, StateSnapshot, ObjectType.STATE)

    approvals = {item["id"]: item for item in rows["merge_approvals"]}
    origins = {item["branch_name"]: item for item in rows["merge_branch_origins"]}
    runs = {item["id"]: item for item in rows["runs"]}
    jobs = {item["id"]: item for item in rows["jobs"]}
    configs = {item["id"]: item for item in rows["agent_configs"]}
    requests = {item["job_id"]: item for item in rows["merge_run_requests"]}
    old_jobs = {item["id"] for item in source["indexes"]["jobs"]}
    old_calls = {item["id"] for item in source["indexes"]["external_calls"]}
    links = {}
    for item in rows["run_commits"]:
        links.setdefault(item["run_id"], []).append(item)
    for items in links.values():
        items.sort(key=lambda item: item["run_order"])
        assert [item["run_order"] for item in items] == list(range(1, len(items) + 1))
    for name, origin in origins.items():
        fork = typed(origin["request_ref"], ForkMergedBranch)
        owned(origin["request_ref"], "merge_branch", origin["id"], "request")
        assert (
            fork.name == name
            and fork.commit_id
            == origin["commit_id"]
            == approvals[origin["approval_id"]]["commit_id"]
        )
        assert fork.approval_id == origin["approval_id"]
    allowed_new_calls = set()
    recovered_roots = set()
    for identifier, row in requests.items():
        job = jobs[identifier]
        payload = typed(job["payload_ref"], JobPayload)
        spec = payload.merged
        assert spec is not None and payload.replay is None and payload.code_revision == revision
        request = typed(row["request_ref"], NewMergedRun)
        owned(row["request_ref"], "merge_run_request", identifier, "request")
        config_row = configs[payload.config_id]
        config = typed(config_row["config_ref"], AgentConfig)
        assert config_row["version"] == row["config_version"] == request.config_version
        assert row["config_id"] == request.config_id == payload.config_id
        assert row["validator_ref"] == request.validator_ref == spec.validator_ref
        assert row["approval_id"] == spec.approval_id
        assert row["commit_id"] == spec.from_commit == request.expected_head
        assert row["initial_state_ref"] == spec.initial_state_ref
        assert request.branch_name == row["branch_name"]
        assert origins[row["branch_name"]]["approval_id"] == spec.approval_id
        assert payload.limits == request.budget and request.budget.within(config.limits)
        config = evolve(config, limits=request.budget)
        validator = typed(spec.validator_ref, ValidatorManifest)
        assert validator.code_revision == revision and validator.dependency_lock_digest == lock
        corpus_data = typed(validator.corpus_ref, FrozenCorpus)
        corpus = Corpus(corpus_data.sources, evaluation_time=corpus_data.evaluation_time)
        assert corpus.digest() == validator.corpus_digest == payload.corpus_digest
        assert tuple(corpus.sources.values()) == payload.sources
        expected = initialize_merged(snapshot(spec.from_commit), config, corpus, read)
        actual = typed(spec.initial_state_ref, StateSnapshot, ObjectType.STATE)
        assert actual == expected
        state = typed(spec.initial_state_ref, StateSnapshot, ObjectType.STATE).data
        agent = parse_contract(encode(state), AgentState)
        assert agent.completed == agent.messages == agent.stopped_executions == ()
        assert (
            agent.usage == BudgetUsage() and agent.pending_tool is None and agent.evaluation is None
        )
        owned(spec.initial_state_ref, "merge_run_input", identifier, "state")
        for artifact in actual.artifacts:
            owned(
                artifact.object_ref,
                "merge_run_input",
                identifier,
                path_reference_role("file", artifact.path),
            )
        current = runs[job["run_id"]]
        assert current["status"] == job["status"] == "completed"
        root = job["origin_run_id"] or job["run_id"]
        family = set()
        cursor = current["id"]
        while True:
            assert cursor not in family and len(family) < 100
            family.add(cursor)
            record = runs[cursor]
            assert (
                record["task_id"] == current["task_id"]
                and record["branch_name"] == request.branch_name
            )
            if cursor == root:
                assert (
                    record["origin_run_id"] is None and record["base_commit_id"] == spec.from_commit
                )
                break
            cursor = record["origin_run_id"]
        if len(family) > 1:
            recovered_roots.add(root)
        allowed_new_calls.update(family)
        checkpoints = {link["commit_id"] for member in family for link in links.get(member, ())}
        for call in rows["external_calls"]:
            if call["run_id"] in family:
                intent = typed(call["intent_ref"], CallIntent, ObjectType.CALL)
                assert intent.checkpoint in checkpoints and call["origin_call_id"] is None
        first = links.get(root, ())
        if first:
            assert snapshot(first[0]["commit_id"]) == actual
    replayed = 0
    for identifier, job in jobs.items():
        if identifier in old_jobs or identifier in requests:
            continue
        payload = typed(job["payload_ref"], JobPayload)
        assert (
            payload.merged is None
            and payload.replay is not None
            and payload.replay.mode == "recorded"
        )
        spec = payload.replay
        source_run = runs[spec.source_run_id]
        path = ([source_run["base_commit_id"]] if source_run["base_commit_id"] else []) + [
            item["commit_id"] for item in links[spec.source_run_id]
        ]
        first, last = path.index(spec.from_commit), path.index(spec.until_commit)
        assert (
            first <= last
            and snapshot(spec.from_commit).data.get("kind") != "merged_research_state/v1"
        )
        selected = path[first : last + 1]
        proof = sorted(
            (item for item in rows["replay_checkpoints"] if item["job_id"] == identifier),
            key=lambda item: item["source_order"],
        )
        assert [item["source_commit_id"] for item in proof] == selected
        assert [item["source_order"] for item in proof] == list(range(len(selected)))
        for item in proof:
            target = typed(item["commit_id"], CommitManifest, ObjectType.COMMIT)
            source_commit = typed(item["source_commit_id"], CommitManifest, ObjectType.COMMIT)
            assert target.state_ref == source_commit.state_ref == item["state_ref"]
        replayed += 1
    new_calls = [item for item in rows["external_calls"] if item["id"] not in old_calls]
    models = tools = mock_bytes = 0
    for call in rows["external_calls"]:
        saved = typed(call["result_ref"], SavedCallOutcome, ObjectType.CALL)
        assert call["status"] == "succeeded" and saved.complete and saved.error is None
        if call["provider"].startswith("llm:"):
            models += 1
            mock_bytes += len(read(call["request_digest"])) + saved.response_bytes
        else:
            tools += 1
    assert all(item["run_id"] in allowed_new_calls for item in new_calls)
    assert all(item["run_id"] not in recovered_roots for item in new_calls)
    process = final["details"].get("process")
    if process is not None:
        assert (
            process["exit_code"] != 0
            and process["pid"] > 0
            and process["source_revision"] == revision
        )
        assert (process["mode"], process["point"]) in {
            ("approve", "merge.head_updated"),
            ("approve", "merge.after_commit"),
            ("run", "before_transaction"),
            ("run", "after_commit"),
        }
    assert all(item["status"] in ("completed", "failed") for item in jobs.values())
    summary.update(
        merged_branches=len(origins),
        merged_jobs=len(requests),
        recorded_jobs=replayed,
        new_calls=len(new_calls),
        models=models,
        tools=tools,
        mock_utf8_bytes=mock_bytes,
        actual_process_kills=int(process is not None),
    )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    results = []
    for path in sorted((args.evidence / "cases").glob("*/final/evidence.json")):
        source = json.loads((path.parent.parent / "source/evidence.json").read_text("utf-8"))
        final = json.loads(path.read_text("utf-8"))
        result = verify_run_pair(
            source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
        )
        result.update(
            case=path.parent.parent.name,
            evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        results.append(result)
    assert len(results) == 9
    summary = dict(
        kind="S11-merged-run-raw-audit/v1",
        source_revision=frozen["source_revision"],
        results=results,
        scope="engineering-not-S11-exit",
    )
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
