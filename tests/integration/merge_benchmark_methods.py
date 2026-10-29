"""冻结三种候选方法及独立内容预期；基线没有发布或执行权限。"""

from time import perf_counter_ns
from uuid import UUID

from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError
from agentgit.core.merge import MergeOrigin
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import EVALUATION_TIME, AgentConfig, AgentState, BudgetLimits, Plan
from agentgit.runtime.validation import render_report, report_from_memory
from agentgit.services.merge import MergePreviewService
from agentgit.services.merge_inputs import base_origin
from agentgit.services.merge_validation import preview_research_merge
from agentgit.services.versions import VersionService

METHODS = ("semantic", "artifact_only", "choose_ours")
_ABSENT = object()


def artifact_dict(item):
    return dict(
        path=item.path,
        media_type=item.media_type,
        status=item.status,
        text=item.content.decode("utf-8") if item.content is not None else None,
        evidence_refs=list(item.evidence_refs),
    )


def inputs(history, repo, payload, rows):
    selected = MergePreviewService(history).bases(repo, payload.ours.head, payload.theirs.head)
    assert selected.selected is not None
    base = base_origin(history, repo, UUID(payload.ours.source_run_id), selected.selected)
    result = []
    for origin in (
        base,
        MergeOrigin(run_id=payload.ours.source_run_id, commit_id=payload.ours.head),
        MergeOrigin(run_id=payload.theirs.source_run_id, commit_id=payload.theirs.head),
    ):
        snapshot = history.restore(repo, origin.commit_id)
        commit = parse_contract(
            history.objects.get(origin.commit_id, ObjectType.COMMIT), CommitManifest
        )
        env = parse_contract(
            history.objects.get(commit.environment_ref, ObjectType.ENVIRONMENT), EnvironmentManifest
        )
        controlled = decode(history.objects.get(env.input_ref, ObjectType.BLOB))
        config = parse_contract(encode(controlled["config"]), AgentConfig)
        state = parse_contract(encode(snapshot.data), AgentState)
        ancestors = set(VersionService(history).ancestors(repo, origin.commit_id))
        family = set()
        current = origin.run_id
        by_id = {row["id"]: row for row in rows["runs"]}
        while current is not None:
            assert current not in family and len(family) < 100
            family.add(current)
            current = by_id[current]["origin_run_id"]
        reads = set()
        for call in rows["external_calls"]:
            if (
                call["provider"] != "tool:source_read/v1"
                or call["status"] != "succeeded"
                or call["run_id"] not in family
            ):
                continue
            intent = parse_contract(
                history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
            )
            request = decode(history.objects.get(intent.request_ref, ObjectType.BLOB))
            evidence = state.working_memory.get(request["source_id"])
            if (
                evidence is not None
                and intent.checkpoint in ancestors
                and evidence.logical_step_id == call["logical_step_id"]
                and evidence.call_index == call["call_index"]
                and evidence.attempt == call["attempt"]
            ):
                reads.add(call["id"])
        result.append(
            dict(
                origin=origin.model_dump(mode="json"),
                state=state,
                config=config,
                snapshot=snapshot,
                read_call_ids=reads,
            )
        )
    return result


def run_method(method, history, repo, payload, original):
    assert method in METHODS
    started = perf_counter_ns()
    record = dict(
        schema_version=1,
        method=method,
        decision="candidate",
        state=None,
        config=None,
        read_call_ids=[],
        artifacts=[],
        conflicts=[],
        error=None,
        validation=None,
        publishable=False,
        execution="candidate_only",
    )
    try:
        if method == "semantic":
            preview = preview_research_merge(
                history,
                repo,
                MergeOrigin(run_id=payload.ours.source_run_id, commit_id=payload.ours.head),
                MergeOrigin(run_id=payload.theirs.source_run_id, commit_id=payload.theirs.head),
                payload.validator_ref,
            )
            state = preview.state.proposed_state
            record.update(
                state=state.model_dump(mode="json") if state else None,
                artifacts=[artifact_dict(item) for item in preview.artifacts],
                conflicts=[item.model_dump(mode="json") for item in preview.validation.conflicts],
                validation=preview.validation.model_dump(mode="json"),
            )
            if state is not None:
                record["read_call_ids"] = sorted({proof.call_id for proof in state.read_proofs})
            if not preview.validation.validated:
                record["decision"] = "conflict"
            record["execution"] = "requires_exact_review"
        elif method == "artifact_only":
            for path in ("report.md", "sources.json"):
                _, artifact = MergePreviewService(history).artifact(
                    repo, payload.ours.head, payload.theirs.head, path
                )
                record["artifacts"].append(artifact_dict(artifact))
                record["conflicts"].extend(
                    item.model_dump(mode="json") for item in artifact.conflicts
                )
            if record["conflicts"]:
                record["decision"] = "conflict"
        else:
            ours = original[1]
            record.update(
                state=ours["state"].model_dump(mode="json"),
                config=ours["config"].model_dump(mode="json"),
                read_call_ids=sorted(ours["read_call_ids"]),
            )
            record["artifacts"] = [
                dict(
                    path=entry.path,
                    media_type=entry.media_type,
                    status="merged",
                    text=history.objects.get(entry.object_ref, ObjectType.BLOB).decode(),
                    evidence_refs=[entry.object_ref],
                )
                for entry in ours["snapshot"].artifacts
            ]
    except AgentGitError as error:
        record.update(decision="input_rejected", error=error.as_dict())
    record["elapsed_ns"] = perf_counter_ns() - started
    return record


def three_way(base, ours, theirs):
    # 预期只用于协议中明确无冲突的十例，不调用产品状态合并器。
    def same(left, right):
        return (
            left is right if left is _ABSENT or right is _ABSENT else encode(left) == encode(right)
        )

    if same(ours, theirs):
        return ours
    if same(ours, base):
        return theirs
    if same(theirs, base):
        return ours
    raise AssertionError("无冲突预期出现未声明的双侧异改")


def merged_table(tables):
    result = {}
    for key in set().union(*(set(table) for table in tables)):
        value = three_way(*(table.get(key, _ABSENT) for table in tables))
        if value is not _ABSENT:
            result[key] = value
    return result


def clean_expectation(original):
    states = [item["state"] for item in original]
    nodes = merged_table(
        [{node.key: node.model_dump(mode="json") for node in state.plan.nodes} for state in states]
    )
    memory = merged_table(
        [
            {key: item.model_dump(mode="json") for key, item in state.working_memory.items()}
            for state in states
        ]
    )
    plan = parse_contract(
        encode(dict(schema_version=1, nodes=[nodes[key] for key in sorted(nodes)])), Plan
    )
    inspection = parse_contract(
        encode(
            dict(
                schema_version=1,
                goal=states[0].goal.model_dump(mode="json"),
                plan=plan.model_dump(mode="json"),
                working_memory=memory,
            )
        ),
        AgentState,
    )
    caps = {}
    for name in BudgetLimits.model_fields:
        if name == "schema_version":
            continue
        finite = [
            getattr(item["config"].limits, name)
            for item in original
            if getattr(item["config"].limits, name) is not None
        ]
        caps[name] = min(finite) if finite else None
    limits = BudgetLimits.model_validate(caps)
    report = report_from_memory(inspection, EVALUATION_TIME)
    return dict(
        nodes=nodes,
        memory=memory,
        goal=states[0].goal.model_dump(mode="json"),
        tools=sorted(set.intersection(*(set(item["config"].tools) for item in original))),
        limits=limits.model_dump(mode="json"),
        read_call_ids=set.union(*(item["read_call_ids"] for item in original[1:])),
        artifacts={
            "sources.json": encode(report.model_dump(mode="json")).decode(),
            "report.md": render_report(report),
        },
    )


def assess(spec, record, expected=None):
    assert record["decision"] in ("candidate", "conflict", "input_rejected")
    if record["decision"] == "conflict":
        assert record["conflicts"]
    if record["decision"] == "input_rejected":
        assert record["error"] is not None
    input_rejected = record["decision"] == "input_rejected"
    incomplete = input_rejected and record["error"]["code"] not in spec["errors"]
    candidate = record["decision"] == "candidate"
    blocked = spec["expected"] == "block"
    result = dict(
        expected=spec["expected"],
        candidate=candidate,
        safe_rejection=blocked and not candidate and not incomplete,
        conflict_detected=blocked and record["decision"] == "conflict",
        input_rejected=input_rejected,
        incomplete=incomplete,
        false_accept=blocked and candidate,
        plan_constraint_unsafe_candidate=blocked and candidate and spec["group"] == "plan",
        effect_unsafe_candidate=blocked and candidate and spec["id"] == "M30",
        clean_artifacts_correct=False,
        clean_content_correct=False,
    )
    if not blocked:
        assert expected is not None
        texts = {
            item["path"]: item["text"] for item in record["artifacts"] if item["status"] == "merged"
        }
        result["clean_artifacts_correct"] = texts == expected["artifacts"]
        state = record["state"]
        if state is not None and state.get("plan") is not None:
            constraints = state.get("constraints") or record["config"]
            result["clean_content_correct"] = bool(
                candidate
                and result["clean_artifacts_correct"]
                and state["goal"] == expected["goal"]
                and {node["key"]: node for node in state["plan"]["nodes"]} == expected["nodes"]
                and state["working_memory"] == expected["memory"]
                and sorted(constraints["tools"]) == expected["tools"]
                and constraints["limits"] == expected["limits"]
                and expected["read_call_ids"] <= set(record["read_call_ids"])
            )
    result["matches_expectation"] = (
        result["safe_rejection"] if blocked else result["clean_content_correct"]
    )
    return result
