"""固定故障规格和真实研究 Run 的测试替身；不进入产品包或 Agent 工作区。"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import sqlalchemy as sa
from pydantic import Field, model_validator

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.diagnosis import EventReference
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, Digest, RunStatus, parse_contract
from agentgit.core.research import (
    AgentConfig,
    BudgetLimits,
    FinalDecision,
    ModelRequest,
    ResearchGoal,
    evolve,
)
from agentgit.core.versions import NewBranch
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.history import TABLES
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService

DATA = Path(__file__).resolve().parents[1] / "fixtures/diagnosis/v1"
OPERATIONS = frozenset(
    {
        "search_empty",
        "search_unknown",
        "search_duplicates",
        "corpus_expired",
        "source_schema",
        "tool_timeout",
        "omit_sources",
        "conflicting_fact",
        "model_malformed",
        "model_empty",
        "markdown_false",
        "claim_value",
        "search_subset",
        "source_identity",
        "corpus_future",
        "plan_cycle",
        "early_final",
        "report_schema",
        "claim_quote",
    }
)


class Annotation(Contract):
    origin: Literal["agent_proposed"]
    review_status: Literal["pending_human"]
    injection: str
    first_visible: str
    repairable_decisions: Annotated[tuple[str, ...], Field(min_length=1, max_length=8)]
    rationale: str


class FaultCase(Contract):
    id: Annotated[str, Field(pattern=r"^D[0-9]{2}$")]
    split: Literal["train", "heldout"]
    family: str
    title: str
    operation: str
    target: str
    expected_error: ErrorCode
    evaluation_error_prefix: str | None
    annotation: Annotation

    @model_validator(mode="after")
    def supported_recipe(self):
        if self.operation not in OPERATIONS:
            raise ValueError("故障规格含未实现的注入操作")
        return self


class Dataset(Contract):
    kind: Literal["diagnosis-benchmark/v1"]
    status: Literal["draft", "frozen"]
    test_only: Literal[True]
    evaluation_time: Literal["2026-10-20T00:00:00Z"]
    random_seed: Literal[0]
    limits: BudgetLimits
    partition_policy: str
    cases: Annotated[tuple[FaultCase, ...], Field(min_length=20, max_length=20)]

    @model_validator(mode="after")
    def fixed_partition(self):
        if [case.id for case in self.cases] != [f"D{index:02}" for index in range(1, 21)]:
            raise ValueError("必须保留全部 20 例及稳定顺序")
        if [case.split for case in self.cases] != ["train"] * 12 + ["heldout"] * 8:
            raise ValueError("必须保持冻结的 12/8 划分")
        if self.limits.max_replans != 0:
            raise ValueError("首版样例固定零重规划，避免注入被后续随机恢复掩盖")
        return self


class DatasetRelease(Contract):
    dataset_id: Literal["diagnosis/v1"]
    cases_sha256: Digest
    fixture_sha256: Digest
    train: tuple[str, ...]
    heldout: tuple[str, ...]
    annotation_status: Literal["pending_human"]
    ranking_status: Literal["not_implemented"]
    frozen_at_utc: str


def verify_release(dataset, payload, fixture_payload, release):
    if (
        release.cases_sha256 != hashlib.sha256(payload).hexdigest()
        or release.fixture_sha256 != hashlib.sha256(fixture_payload).hexdigest()
        or release.train != tuple(case.id for case in dataset.cases if case.split == "train")
        or release.heldout != tuple(case.id for case in dataset.cases if case.split == "heldout")
    ):
        raise AgentGitError(
            ErrorCode.REPLAY_MISMATCH, "冻结故障规格、划分或注入器摘要不符，必须建立新版本"
        )


def load_dataset():
    payload = (DATA / "cases.json").read_bytes()
    dataset = parse_contract(payload, Dataset)
    if dataset.status == "frozen":
        release = parse_contract((DATA / "release.json").read_bytes(), DatasetRelease)
        verify_release(dataset, payload, Path(__file__).read_bytes(), release)
    return dataset


@dataclass
class Faults:
    case: FaultCase
    enabled: bool
    receipts: list[dict] = field(default_factory=list)

    def record(self, step, before, after, *, simulated_timeout=False):
        self.receipts.append(
            {
                "logical_step_id": step,
                "original_response_ref": object_digest(ObjectType.BLOB, before)
                if before is not None
                else None,
                "injected_response_ref": object_digest(ObjectType.BLOB, after),
                "simulated_timeout": simulated_timeout,
            }
        )

    def corpus(self):
        corpus = Corpus.bundled()
        if not self.enabled or not self.case.operation.startswith("corpus_"):
            return corpus
        changes = (
            {"valid_until": "2026-09-30T00:00:00Z"}
            if self.case.operation == "corpus_expired"
            else {"collected_at": "2026-11-01T00:00:00Z"}
        )
        return Corpus(
            tuple(
                evolve(source, **changes) if source.id == self.case.target else source
                for source in corpus.sources.values()
            )
        )

    def model(self, payload):
        request = parse_contract(payload, ModelRequest)
        operation = self.case.operation
        step = f"model-{request.state.usage.steps + 1}"
        original = MockResearchModel().invoke(payload)
        if not self.enabled:
            return original
        if request.phase == "plan" and operation in {
            "model_empty",
            "model_malformed",
            "omit_sources",
            "plan_cycle",
        }:
            decision = decode(original)
            if operation == "model_empty":
                changed = b""
            elif operation == "model_malformed":
                changed = b'{"schema_version":1,"kind":"plan","plan":'
            else:
                if operation == "omit_sources":
                    decision["plan"]["nodes"] = [
                        node for node in decision["plan"]["nodes"] if node["key"] != "write-sources"
                    ]
                else:
                    decision["plan"]["nodes"][0]["dependencies"] = ["read-0"]
                changed = encode(decision)
        elif request.phase == "act":
            decision = decode(original)
            if operation == "early_final" and decision.get("node_key") == "search":
                changed = encode(FinalDecision(summary="故障替身过早结束").model_dump(mode="json"))
            elif (
                decision.get("kind") == "tool"
                and operation == "markdown_false"
                and decision["arguments"].get("path") == "report.md"
            ):
                decision["arguments"]["text"] = "三个框架均支持无限规模；本句没有可核验来源。"
                changed = encode(decision)
            elif (
                decision.get("kind") == "tool"
                and decision["arguments"].get("path") == "sources.json"
                and operation in {"claim_value", "claim_quote", "report_schema"}
            ):
                report = decode(decision["arguments"]["text"].encode())
                if operation == "report_schema":
                    report["schema_version"] = 2
                else:
                    report["claims"][0]["value" if operation == "claim_value" else "quote"] = (
                        "冻结资料没有支持的断言"
                    )
                decision["arguments"]["text"] = encode(report).decode()
                changed = encode(decision)
            else:
                return original
        else:
            return original
        self.record(step, original, changed)
        return changed

    def tools(self, session):
        original = session.tools.invoke

        def invoke(name, arguments):
            operation = self.case.operation
            applicable = self.enabled and (
                (name == "corpus_search" and operation.startswith("search_"))
                or (
                    name == "source_read"
                    and arguments.get("source_id") == self.case.target
                    and operation
                    in {"tool_timeout", "source_schema", "source_identity", "conflicting_fact"}
                )
            )
            if not applicable:
                return original(name, arguments)
            step = session.state.pending_tool.node_key
            if operation == "tool_timeout":
                self.record(step, None, b"", simulated_timeout=True)
                raise AgentGitError(
                    ErrorCode.TOOL_ERROR, "确定性读取超时替身；未执行网络请求或真实等待"
                )
            before = original(name, arguments)
            result = decode(before)
            if operation == "search_empty":
                changed = b""
            else:
                if operation == "search_unknown":
                    result["source_ids"][0] = "missing-source"
                elif operation == "search_duplicates":
                    result["source_ids"].append(result["source_ids"][0])
                elif operation == "search_subset":
                    result["source_ids"] = result["source_ids"][:4]
                elif operation == "source_schema":
                    result["schema_version"] = 2
                elif operation == "source_identity":
                    result["source"]["id"] = "different-source"
                elif operation == "conflicting_fact":
                    fact = {**result["source"]["facts"][0], "value": "相互冲突的调度方式"}
                    result["source"]["facts"].append(fact)
                changed = encode(result)
            self.record(step, before, changed)
            return changed

        session.tools.invoke = invoke


class FixtureModel:
    # 两侧使用同一替身协议签名；注入开关和规格另由测试证据记录。
    signature = object_digest(
        ObjectType.CALL, encode({"schema_version": 1, "adapter": "diagnosis-fixture-model/v1"})
    )

    def __init__(self, faults):
        self.faults = faults

    def invoke(self, payload):
        return self.faults.model(payload)


def resolve_selector(history, session, selector):
    kind, value = selector.split(":", 1)
    if kind == "corpus":
        return {
            "kind": "frozen_input",
            "input_ref": session.environment.input_ref,
            "source_id": value,
        }
    candidates = []
    for event in history.events(session.repository_id, session.run_id, limit=1000):
        if event["type"] not in {"llm.completed", "tool.completed", "call.failed"}:
            continue
        payload = history.objects.get_json(event["payload_ref"], ObjectType.EVENT)["data"]
        occurrence = payload.get("occurrence", {})
        if (
            kind == "tool"
            and occurrence.get("provider", "").startswith("tool:")
            and event["logical_step_id"] == value
        ):
            candidates.append(event)
        elif kind == "model" and occurrence.get("provider", "").startswith("llm:"):
            request = parse_contract(
                history.objects.get(occurrence["request_digest"], ObjectType.BLOB), ModelRequest
            )
            ready = request.state.plan.ready(request.state.completed) if request.state.plan else ()
            if (value == "plan" and request.phase == "plan") or (
                value != "plan" and request.phase == "act" and ready and ready[0].key == value
            ):
                candidates.append(event)
    if len(candidates) != 1:
        raise AssertionError(f"标签 {selector} 必须唯一解析到已录制事件，实际 {len(candidates)}")
    event = candidates[0]
    return EventReference(
        run_id=str(session.run_id), seq=event["seq"], event_id=event["id"]
    ).model_dump(mode="json")


def run_case(history, tmp_path, case, dataset):
    clean = Faults(case, False)
    research = ResearchService(history, tmp_path / "reference", corpus=clean.corpus())
    repo = history.create_repository(f"定位样例 {case.id}")
    config = research.create_config(repo, AgentConfig(limits=dataset.limits))
    goal = ResearchGoal(text="比较三个 fixture 框架")
    reference_task = research.create_task(repo, config, goal)
    reference = research.start(repo, reference_task)
    clean.tools(reference)
    passed = reference.execute(FixtureModel(clean))
    assert passed.status == RunStatus.COMPLETED and passed.evaluation.passed
    assert not clean.receipts
    with history.engine.connect() as connection:
        links = TABLES["run_commits"]
        anchor = connection.execute(
            sa.select(links.c.commit_id)
            .where(links.c.run_id == reference.run_id)
            .order_by(links.c.run_order)
            .limit(1)
        ).scalar_one()
    source_branch = history.branch(repo)
    VersionService(history).fork(
        repo,
        NewBranch(
            source_run_id=str(reference.run_id),
            from_commit=anchor,
            name=case.id,
            expected_version=0,
        ),
        actor="local-operator",
    )
    faults = Faults(case, True)
    failure_service = ResearchService(history, tmp_path / "failure", corpus=faults.corpus())
    task = failure_service.create_task(repo, config, goal)
    failed = failure_service.start(repo, task, branch_name=case.id)
    faults.tools(failed)
    result = failed.execute(FixtureModel(faults))
    assert result.status == RunStatus.FAILED
    assert result.error_code == case.expected_error.value
    if case.evaluation_error_prefix is not None:
        assert result.evaluation is not None and not result.evaluation.passed
        assert any(
            error.startswith(case.evaluation_error_prefix) for error in result.evaluation.errors
        )
    assert len(faults.receipts) == (0 if case.operation.startswith("corpus_") else 1)
    graph = BehaviorService(history).trace(repo, failed.run_id)
    assert graph.scope_complete
    assert history.branch(repo) == source_branch
    resolved = {
        "injection": resolve_selector(history, failed, case.annotation.injection),
        "first_visible": resolve_selector(history, failed, case.annotation.first_visible),
        "repairable_decisions": [
            resolve_selector(history, failed, selector)
            for selector in case.annotation.repairable_decisions
        ],
    }
    for receipt in faults.receipts:
        found = [
            node
            for node in graph.nodes
            if node.phase == "call.result" and node.logical_step_id == receipt["logical_step_id"]
        ]
        assert len(found) == 1 and found[0].observation_ref == receipt["injected_response_ref"]
    return {
        "case_id": case.id,
        "split": case.split,
        "test_only": True,
        "annotation_origin": case.annotation.origin,
        "annotation_review_status": case.annotation.review_status,
        "reference_run_id": str(reference.run_id),
        "failed_run_id": str(failed.run_id),
        "repository_id": str(repo),
        "common_ancestor": anchor,
        "status": result.status.value,
        "error_code": result.error_code,
        "evaluation": result.evaluation.model_dump(mode="json") if result.evaluation else None,
        "receipts": faults.receipts,
        "resolved_labels": resolved,
        "failed_usage": result.usage.model_dump(mode="json"),
        "graph": graph.model_dump(mode="json"),
        "reference_graph": BehaviorService(history)
        .trace(repo, reference.run_id)
        .model_dump(mode="json"),
        "corpus_changed": failure_service.corpus.digest() != research.corpus.digest(),
        "fault_fixture_sha256": hashlib.sha256((DATA / "cases.json").read_bytes()).hexdigest(),
    }
