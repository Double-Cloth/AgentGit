"""离线审查三十例构造的真实输入、调用和只读边界；不冒充正式候选/基线审计。"""

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.jobs import JobPayload
from agentgit.core.models import CallIntent, CommitManifest, parse_contract
from agentgit.core.research import AgentConfig, SavedCallOutcome, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import LiveOverrides
from agentgit.replay.live import prepare_live


def verify(source, final, frozen):
    assert source["repository_id"] == final["repository_id"]
    assert source["source_revision"] == final["source_revision"] == frozen["source_revision"]
    assert source["indexes"] == final["indexes"] and source["objects"] == final["objects"]
    rows = final["indexes"]
    repo = final["repository_id"]
    assert all(row["repository_id"] == repo for values in rows.values() for row in values)
    payloads = {}
    for ref, entry in final["objects"].items():
        payload = base64.b64decode(entry["base64"], validate=True)
        assert object_digest(ObjectType(entry["object_type"]), payload) == ref
        assert len(payload) == entry["byte_length"]
        payloads[ref] = payload
    assert sum(map(len, payloads.values())) == source["raw_bytes"] == final["raw_bytes"]
    assert all(row["object_id"] in payloads for row in rows["object_refs"])
    refs = {
        (row["owner_type"], row["owner_id"], row["role"], row["object_id"])
        for row in rows["object_refs"]
    }

    def read(ref, kind=ObjectType.BLOB):
        assert final["objects"][ref]["object_type"] == kind.value
        return payloads[ref]

    def typed(ref, model, kind=ObjectType.BLOB):
        return parse_contract(read(ref, kind), model)

    commits = {row["id"]: row for row in rows["commits"]}
    runs = {row["id"]: row for row in rows["runs"]}
    configs = {row["id"]: row for row in rows["agent_configs"]}
    jobs = {row["id"]: row for row in rows["jobs"]}
    replay = {row["job_id"]: row for row in rows["replay_requests"]}
    paths = {}
    for identifier, run in runs.items():
        links = sorted(
            (row for row in rows["run_commits"] if row["run_id"] == identifier),
            key=lambda row: row["run_order"],
        )
        assert [row["run_order"] for row in links] == list(range(1, len(links) + 1))
        paths[identifier] = ([run["base_commit_id"]] if run["base_commit_id"] else []) + [
            row["commit_id"] for row in links
        ]
        previous = run["base_commit_id"]
        for link in links:
            manifest = typed(link["commit_id"], CommitManifest, ObjectType.COMMIT)
            indexed = commits[link["commit_id"]]
            assert manifest.repository_id == repo and manifest.state_ref == indexed["state_ref"]
            assert manifest.parents == ((previous,) if previous else ())
            parents = sorted(
                (row for row in rows["commit_parents"] if row["commit_id"] == link["commit_id"]),
                key=lambda row: row["parent_order"],
            )
            assert [row["parent_id"] for row in parents] == list(manifest.parents)
            previous = link["commit_id"]
        assert previous == run["checkpoint_commit_id"]

    def snapshot(commit):
        return typed(commits[commit]["state_ref"], StateSnapshot, ObjectType.STATE)

    details = final["details"]["construction"]
    spec = details["case"]
    assert spec == source["details"]["case"]
    assert len(details["branches"]) == len(jobs) == len(replay) == 2
    assert len(runs) == 3 and not rows["merge_proposals"] and not rows["merge_approvals"]
    for branch in details["branches"]:
        identifier = branch["submitted"]["job_id"]
        job = jobs[identifier]
        assert ("job", identifier, "payload", job["payload_ref"]) in refs
        payload = typed(job["payload_ref"], JobPayload)
        live = payload.replay
        assert live is not None and live.mode == "live" and payload.merged is None
        assert payload.code_revision == frozen["source_revision"]
        assert live.from_commit == details["base"] == replay[identifier]["from_commit_id"]
        assert live.source_run_id == details["root_run_id"] == replay[identifier]["source_run_id"]
        assert job["run_id"] == branch["submitted"]["run_id"]
        assert runs[job["run_id"]]["agent_config_id"] == payload.config_id
        config = typed(configs[payload.config_id]["config_ref"], AgentConfig)
        assert (
            "agent_config",
            payload.config_id,
            "config",
            configs[payload.config_id]["config_ref"],
        ) in refs
        overrides = parse_contract(encode(branch["overrides"]), LiveOverrides)
        assert config == overrides.config
        if payload.limits is not None:
            config = evolve(config, limits=payload.limits)
        prepared = prepare_live(snapshot(live.from_commit), config, overrides, read)
        actual = typed(live.initial_state_ref, StateSnapshot, ObjectType.STATE)
        assert actual == prepared.snapshot and live.budget_baseline == prepared.budget_baseline
        assert snapshot(paths[job["run_id"]][1]) == actual
        assert job["status"] == runs[job["run_id"]]["status"]
    models = tools = mock_bytes = unknown = 0
    for call in rows["external_calls"]:
        intent = typed(call["intent_ref"], CallIntent, ObjectType.CALL)
        assert ("external_call", call["id"], "intent", call["intent_ref"]) in refs
        assert ("external_call", call["id"], "request", call["request_digest"]) in refs
        assert intent.checkpoint in paths[call["run_id"]]
        assert intent.request_ref == call["request_digest"]
        for name in (
            "logical_step_id",
            "call_index",
            "attempt",
            "provider",
            "request_digest",
            "signature_digest",
        ):
            assert getattr(intent.occurrence, name) == call[name]
        assert intent.effect.value == call["effect"]
        if call["provider"] == "fixture:payment/v1":
            assert spec["id"] == "M30" and call["status"] == "outcome_unknown"
            assert call["result_ref"] is None
            approved = [row for row in rows["approvals"] if row["consumed_call_id"] == call["id"]]
            assert len(approved) == 1 and approved[0]["decision"] == "approve"
            assert approved[0]["status"] == "consumed"
            unknown += 1
            continue
        assert call["status"] == "succeeded" and call["origin_call_id"] is None
        saved = typed(call["result_ref"], SavedCallOutcome, ObjectType.CALL)
        assert ("external_call", call["id"], "result", call["result_ref"]) in refs
        assert ("external_call", call["id"], "response", saved.response_ref) in refs
        assert saved.complete and saved.error is None
        assert saved.response_bytes == len(read(saved.response_ref))
        if call["provider"] == "llm:mock-research/v1":
            models += 1
            mock_bytes += len(read(call["request_digest"])) + saved.response_bytes
        else:
            assert call["provider"] in {
                "tool:corpus_search/v1",
                "tool:source_read/v1",
                "tool:artifact_write/v1",
            }
            tools += 1
    assert unknown == int(spec["id"] == "M30")
    return dict(
        case=spec["id"],
        models=models,
        tools=tools,
        mock_utf8_bytes=mock_bytes,
        payment_unknown=unknown,
        preparation_only=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads((args.evidence / "frozen/snapshot.json").read_text("utf-8"))
    asset_name = "tests/fixtures/merge/v1/cases.json"
    asset_bytes = (args.evidence / "frozen" / asset_name).read_bytes()
    assert hashlib.sha256(asset_bytes).hexdigest() == frozen["files"][asset_name]
    assets = {item["id"]: item for item in json.loads(asset_bytes)["cases"]}
    assert sorted(assets) == [f"M{index:02d}" for index in range(1, 31)]
    results = []
    for path in sorted((args.evidence / "cases").glob("*/final/evidence.json")):
        source = json.loads((path.parent.parent / "source/evidence.json").read_text("utf-8"))
        final = json.loads(path.read_text("utf-8"))
        assert source["details"]["case"] == assets[path.parent.parent.name]
        result = verify(source, final, frozen)
        result["evidence_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        # 同时改两份只读边界快照，确保拒绝来自索引/不可变请求核验。
        bad_source, bad_final = copy.deepcopy(source), copy.deepcopy(final)
        bad_source["indexes"]["replay_requests"][0]["source_run_id"] = (
            "00000000-0000-0000-0000-000000000000"
        )
        bad_final["indexes"] = bad_source["indexes"]
        try:
            verify(bad_source, bad_final, frozen)
        except AssertionError:
            result["substituted_source_rejected"] = True
        else:
            raise AssertionError("替换来源未被拒绝")
        results.append(result)
    assert len(results) == 30
    assert sorted(row["case"] for row in results) == sorted(assets)
    summary = dict(
        kind="S11-merge-input-construction-audit/v1",
        results=results,
        source_revision=frozen["source_revision"],
        scope="construction-only/not-formal-candidate-or-baseline-results",
    )
    with args.destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            dict(
                cases=len(results),
                totals={
                    name: sum(row[name] for row in results)
                    for name in ("models", "tools", "mock_utf8_bytes", "payment_unknown")
                },
            )
        )
    )


if __name__ == "__main__":
    main()
