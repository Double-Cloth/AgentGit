"""离线定位评估：分析不接收 gold，计分保留所有失败和并列。"""

import base64
from collections import Counter
from datetime import datetime
from fractions import Fraction
from time import perf_counter_ns
from unittest.mock import patch

from diagnosis_evidence import verify_repository_evidence

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.debug_reports import MAX_ASSERTIONS, FailureAssertion
from agentgit.core.diagnosis import EventReference
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CommitManifest, EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import AgentState
from agentgit.core.snapshots import EventManifest, EventPayload, StateSnapshot
from agentgit.diagnosis import candidates
from agentgit.diff.behavioral import behavioral_diff


class FrozenEvidence:
    def __init__(self, bundle, failed, reference):
        # 不转交样例 ID、标签或划分，只核对原始内容和所属索引。
        self.bundle = {
            key: bundle[key]
            for key in ("test_only", "repository_id", "indexes", "objects", "raw_bytes")
        }
        view = {
            "repository_id": failed.repository_id,
            "graph": failed.model_dump(mode="json"),
            "reference_graph": (reference or failed).model_dump(mode="json"),
        }
        verify_repository_evidence(self.bundle, view)
        self.repo = failed.repository_id
        self.owners = {
            (row["owner_type"], row["owner_id"], row["role"], row["object_id"])
            for row in self.bundle["indexes"]["object_refs"]
        }

    def owned(self, owner_type, owner, role, ref, kind):
        if (owner_type, owner, role, ref) not in self.owners:
            raise AgentGitError(ErrorCode.NOT_FOUND, "离线证据缺少对应实体验证归属")
        item = self.bundle["objects"][ref]
        if item["object_type"] != kind.value:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线证据对象类型不同")
        return base64.b64decode(item["base64"], validate=True)

    def head(self, graph):
        runs = [row for row in self.bundle["indexes"]["runs"] if row["id"] == graph.run_id]
        if len(runs) != 1 or any(
            runs[0][field] != value
            for field, value in (
                ("status", graph.run_status),
                ("checkpoint_commit_id", graph.head_commit),
                ("last_event_seq", graph.as_of_seq),
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线 Run 索引与捕获轨迹不同")
        head = graph.head_commit
        if head is None:
            return None
        manifest = parse_contract(
            self.owned("commit", head, "manifest", head, ObjectType.COMMIT), CommitManifest
        )
        commits = [row for row in self.bundle["indexes"]["commits"] if row["id"] == head]
        if (
            len(commits) != 1
            or manifest.repository_id != self.repo
            or any(
                getattr(manifest, key) != commits[0][key]
                for key in ("state_ref", "environment_ref", "event_manifest_ref")
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线检查点索引不符")
        events = parse_contract(
            self.owned(
                "commit", head, "events", manifest.event_manifest_ref, ObjectType.EVENT_MANIFEST
            ),
            EventManifest,
        )
        if [event.seq for event in events.events] != list(
            range(manifest.event_range.first_seq, manifest.event_range.last_seq + 1)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线检查点事件范围不完整")
        by_seq = {node.event.seq: node for node in graph.nodes}
        for event in events.events:
            if (
                event.seq not in by_seq
                or by_seq[event.seq].payload_ref != event.payload_ref
                or by_seq[event.seq].event_type != event.type
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线终态事件与清单不符")
        snapshot = parse_contract(
            self.owned("commit", head, "state", manifest.state_ref, ObjectType.STATE), StateSnapshot
        )
        environment = parse_contract(
            self.owned(
                "commit", head, "environment", manifest.environment_ref, ObjectType.ENVIRONMENT
            ),
            EnvironmentManifest,
        )
        self.owned("repository", self.repo, "input", environment.input_ref, ObjectType.BLOB)
        return manifest, snapshot

    def terminal(self, graph, head):
        seq = head[0].event_range.last_seq
        snapshots = [
            row
            for row in self.bundle["indexes"]["snapshots"]
            if row["run_id"] == graph.run_id and row["commit_id"] == graph.head_commit
        ]
        node = next((node for node in graph.nodes if node.event.seq == seq), None)
        if (
            len(snapshots) != 1
            or snapshots[0]["event_seq"] != seq
            or seq != graph.as_of_seq
            or node is None
            or not node.payload_verified
            or any(gap.event == node.event for gap in graph.gaps)
        ):
            return None
        payload = parse_contract(
            self.owned("event", node.event.key, "payload", node.payload_ref, ObjectType.EVENT),
            EventPayload,
        )
        return node, payload.data

    def assertions(self, graph):
        if graph.run_status not in {"failed", "budget_exhausted"}:
            return ()
        head = self.head(graph)
        if head is None or "goal" not in head[1].data or "plan_version" not in head[1].data:
            return ()
        state = parse_contract(encode(head[1].data), AgentState)
        if state.status.value != graph.run_status:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线失败终态不符")
        terminal = self.terminal(graph, head)
        if terminal is None:
            return ()
        node, data = terminal
        if (
            node.event_type == "run.evaluated"
            and state.status == RunStatus.FAILED
            and state.evaluation is not None
            and not state.evaluation.passed
        ):
            if data.get("evaluation") != state.evaluation.model_dump(mode="json"):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线失败硬断言与快照不同")
            codes = tuple(dict.fromkeys(state.evaluation.errors)) or ("validation_failed",)
            if len(codes) > MAX_ASSERTIONS or any(len(code) > 200 for code in codes):
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "离线失败硬断言超过上限")
            return tuple(
                FailureAssertion(event=node.event, origin="research_validator", code=code)
                for code in codes
            )
        if node.event_type == "run.stopped" and state.error_code is not None:
            if (
                not isinstance(data.get("error"), dict)
                or data["error"].get("code") != state.error_code
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线 Runtime 停止原因不同")
            return (
                FailureAssertion(event=node.event, origin="runtime_stop", code=state.error_code),
            )
        return ()

    def validate_reference(self, graph):
        head = self.head(graph)
        if graph.run_status != "completed" or not graph.scope_complete or head is None:
            raise AgentGitError(ErrorCode.VALIDATION, "离线参考缺少成功终态")
        state = parse_contract(encode(head[1].data), AgentState)
        terminal = self.terminal(graph, head)
        if (
            state.status != RunStatus.COMPLETED
            or state.evaluation is None
            or not state.evaluation.passed
            or terminal is None
            or terminal[0].event_type != "run.evaluated"
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "离线参考没有匹配的通过验收")
        if terminal[1].get("evaluation") != state.evaluation.model_dump(mode="json"):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "离线参考验收事件与快照不同")

    def first_explicit_failure(self, graph):
        indexed = {(row["run_id"], row["seq"]): row for row in self.bundle["indexes"]["events"]}
        errors = []
        for node in graph.nodes:
            if not node.payload_verified:
                continue
            data = parse_contract(
                self.owned("event", node.event.key, "payload", node.payload_ref, ObjectType.EVENT),
                EventPayload,
            ).data
            failed = isinstance(data.get("error"), dict) and bool(data["error"].get("code"))
            failed = failed or (
                isinstance(data.get("evaluation"), dict)
                and data["evaluation"].get("passed") is False
            )
            if failed:
                row = indexed[(graph.run_id, node.event.seq)]
                clock = datetime.fromisoformat(row["recorded_at"])
                if clock.utcoffset() is None:
                    raise AgentGitError(ErrorCode.VALIDATION, "时间基线缺少有时区的事件时间")
                errors.append((clock, node.event.seq, node.event))
        return min(errors, key=lambda item: (item[0], item[1]))[2] if errors else None


def analyze_evidence(failed, reference, bundle):
    started = perf_counter_ns()
    evidence = FrozenEvidence(bundle, failed, reference)
    assertions = evidence.assertions(failed)
    comparison = None
    if reference is not None:
        evidence.validate_reference(reference)
        comparison = behavioral_diff(failed, reference)
    baseline = evidence.first_explicit_failure(failed)
    loaded = perf_counter_ns()
    visits = Counter()
    original = candidates._distances

    def measured(starts, neighbors):
        result = original(starts, neighbors)
        visits["distance_node_visits"] += len(result)
        visits["distance_edge_checks"] += sum(len(neighbors[key]) for key in result)
        return result

    with patch.object(candidates, "_distances", measured):
        diagnosis = candidates.static_diagnosis(failed, assertions, comparison)
    ended = perf_counter_ns()
    return {
        "diagnosis": diagnosis.model_dump(mode="json"),
        "baseline_first_failure": baseline.model_dump(mode="json") if baseline else None,
        "effort": {
            "distance_node_visits": visits["distance_node_visits"],
            "distance_edge_checks": visits["distance_edge_checks"],
            "candidate_count": len(diagnosis.candidates),
            "model_calls": 0,
            "tool_calls": 0,
        },
        "timing_ns": {
            "evidence_and_alignment": loaded - started,
            "ranking": ended - loaded,
            "total": ended - started,
        },
    }


def _event_key(value):
    if value is None or value.get("kind") == "frozen_input":
        return None
    reference = parse_contract(encode(value), EventReference)
    return reference.run_id, reference.seq, reference.event_id


def score_targets(analysis, labels, graph):
    available = {_event_key(node.event.model_dump(mode="json")) for node in graph.nodes}
    targets = {
        "injection_point": [labels["injection"]],
        "first_visible": [labels["first_visible"]],
        "repairable_decision": labels["repairable_decisions"],
    }
    candidates = analysis["diagnosis"]["candidates"]
    located = analysis["diagnosis"]["status"] == "candidates_available"
    output = {}
    for name, values in targets.items():
        keys = {_event_key(value) for value in values}
        supported = None not in keys
        keys.discard(None)
        if any(key not in available for key in keys):
            raise AgentGitError(ErrorCode.NOT_FOUND, "计分标签不属于该样例的精确失败事件")
        row = {
            "target_supported": supported,
            "baseline_hit": _event_key(analysis["baseline_first_failure"]) in keys,
        }
        for limit in (1, 3):
            certain = {
                _event_key(item["event"])
                for item in candidates
                if item["rank"] + item["tied_count"] - 1 <= limit
            }
            possible = {_event_key(item["event"]) for item in candidates if item["rank"] <= limit}
            row[f"top{limit}_hit"] = located and bool(keys & certain)
            row[f"top{limit}_boundary_ambiguity"] = (
                located and bool(keys & possible) and not row[f"top{limit}_hit"]
            )
        output[name] = row
    visible = {_event_key(item) for item in analysis["diagnosis"]["observed_anomalies"]}
    output["first_visible_observation_covered"] = _event_key(labels["first_visible"]) in visible
    return output


def exact_mean(values):
    known = [value for value in values if value is not None]
    if not known:
        return {"known": 0, "missing": len(values), "mean": None}
    value = Fraction(sum(known), len(known))
    return {
        "known": len(known),
        "missing": len(values) - len(known),
        "mean": {"numerator": str(value.numerator), "denominator": str(value.denominator)},
    }


def aggregate(rows, mode):
    statuses = Counter(row.get(mode, {}).get("status", "missing") for row in rows)
    result = {
        "scheduled": len(rows),
        "statuses": dict(statuses),
        "unlocated": sum(
            row.get(mode, {}).get("status") != "analyzed"
            or row.get(mode, {}).get("analysis", {}).get("diagnosis", {}).get("status")
            != "candidates_available"
            for row in rows
        ),
        "targets": {},
    }
    for target in ("injection_point", "first_visible", "repairable_decision"):
        result["targets"][target] = {
            "denominator": len(rows),
            **{
                metric: sum(
                    bool(row.get(mode, {}).get("scores", {}).get(target, {}).get(metric))
                    for row in rows
                )
                for metric in (
                    "top1_hit",
                    "top3_hit",
                    "baseline_hit",
                    "top1_boundary_ambiguity",
                    "top3_boundary_ambiguity",
                )
            },
            "unsupported": sum(
                not row.get(mode, {})
                .get("scores", {})
                .get(target, {})
                .get("target_supported", False)
                for row in rows
            ),
        }
    result["effort"] = {
        metric: exact_mean(
            [row.get(mode, {}).get("analysis", {}).get("effort", {}).get(metric) for row in rows]
        )
        for metric in (
            "distance_node_visits",
            "distance_edge_checks",
            "candidate_count",
            "model_calls",
            "tool_calls",
        )
    }
    result["timing_ns"] = {
        metric: exact_mean(
            [row.get(mode, {}).get("analysis", {}).get("timing_ns", {}).get(metric) for row in rows]
        )
        for metric in ("evidence_and_alignment", "ranking", "total")
    }
    result["first_visible_observation_coverage"] = {
        "covered": sum(
            bool(row.get(mode, {}).get("scores", {}).get("first_visible_observation_covered"))
            for row in rows
        ),
        "denominator": len(rows),
    }
    return result
