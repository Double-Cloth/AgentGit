"""改变同参搜索录制顺序使 fixture 通过，仍禁止正式配置采纳。"""

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from repair_case_execution import drive, reduce_and_open, register_suite, worker
from sqlalchemy.exc import DBAPIError

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.repair_reviews import DecideRepairReview, RepairReviewDecision
from agentgit.core.research import ModelRequest, ResearchGoal, ToolDecision, evolve, parse_decision
from agentgit.repair.policy import mock_budget
from agentgit.services.management import ManagementService
from agentgit.services.repair_reviews import TABLES, RepairReviewService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService

pytestmark = pytest.mark.integration


def binding():
    return "observation-adapter-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ObservationOrderModel(MockResearchModel):
    """仅对预先声明的故障任务依赖输入顺序，不读取 trial 阶段或验收结果。"""

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert binding() in request.state.goal.text
        decision = parse_decision(super().invoke(payload))
        if (
            request.state.goal.text.startswith("observation-order-fault;")
            and request.state.search_results == tuple(sorted(request.state.search_results))
            and isinstance(decision, ToolDecision)
            and decision.arguments.get("path") == "sources.json"
        ):
            decision = evolve(
                decision, arguments={**decision.arguments, "path": "fixture-sources.json"}
            )
        return encode(decision.model_dump(mode="json"))


class ReversedSearchRecording(ToolRegistry):
    """实际生成不同顺序的完整只读录制，来源内容与语料均不修改。"""

    def invoke(self, name, arguments):
        raw = super().invoke(name, arguments)
        if name == "corpus_search":
            response = decode(raw)
            response["source_ids"] = list(reversed(response["source_ids"]))
            return encode(response)
        return raw


def count_configs(history):
    with history.engine.connect() as connection:
        return connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["agent_configs"])
        ).scalar_one()


def test_real_observation_passes_six_plus_four_but_can_only_save_exact_experiment(
    history, tmp_path
):
    research = ResearchService(history, tmp_path / "observation")
    repo = history.create_repository("仅观测改变的伪修复")
    config = research.create_config(repo)
    donor = research.start(
        repo,
        research.create_task(repo, config, ResearchGoal(text="observation-donor;" + binding())),
    )
    donor.tools = ReversedSearchRecording(research.corpus, donor.workspace, donor.config)
    assert donor.execute(ObservationOrderModel()).status.value == "completed"
    failed = research.start(
        repo,
        research.create_task(
            repo, config, ResearchGoal(text="observation-order-fault;" + binding())
        ),
    )
    assert failed.execute(ObservationOrderModel()).status.value == "failed"
    validator = ValidatorService(history).freeze(repo, failed.state.goal, research.corpus)
    with history.engine.connect() as connection:
        calls = TABLES["external_calls"]
        original = (
            connection.execute(
                sa.select(calls).where(
                    calls.c.run_id == failed.run_id, calls.c.provider == "tool:corpus_search/v1"
                )
            )
            .mappings()
            .one()
        )
        replacement = (
            connection.execute(
                sa.select(calls).where(
                    calls.c.run_id == donor.run_id, calls.c.provider == "tool:corpus_search/v1"
                )
            )
            .mappings()
            .one()
        )
        events = TABLES["events"]
        event = connection.execute(
            sa.select(events.c.id).where(
                events.c.run_id == failed.run_id,
                events.c.logical_step_id == original["logical_step_id"],
                events.c.type == "tool.started",
            )
        ).scalar_one()
        links = TABLES["run_commits"]
        base = connection.execute(
            sa.select(links.c.commit_id)
            .where(links.c.run_id == failed.run_id)
            .order_by(links.c.run_order)
            .limit(1)
        ).scalar_one()
    captured = parse_contract(
        history.objects.get(original["intent_ref"], ObjectType.CALL), CallIntent
    )
    # 从新模型决策前开始，不能把已暂存工具参数当作独立确认样本。
    assert base != captured.checkpoint
    old_saved = history.objects.get_json(original["result_ref"], ObjectType.CALL)
    new_saved = history.objects.get_json(replacement["result_ref"], ObjectType.CALL)
    old = history.objects.get_json(old_saved["response_ref"], ObjectType.BLOB)
    changed = history.objects.get_json(new_saved["response_ref"], ObjectType.BLOB)
    assert old["source_ids"] == list(reversed(changed["source_ids"])) and old != changed
    assert (
        original["request_digest"] == replacement["request_digest"]
        and original["signature_digest"] == replacement["signature_digest"]
    )
    manager = ManagementService(research)
    made = manager.create_repair_search(
        repo,
        NewRepairSearch.model_validate_json(
            encode(
                dict(
                    schema_version=1,
                    failed_run_id=str(failed.run_id),
                    base_commit=base,
                    validator_ref=validator,
                    targets=[
                        dict(
                            schema_version=1,
                            event_id=event,
                            replacement_call_ids=[str(replacement["id"])],
                        )
                    ],
                    budget=mock_budget(failed.config).model_dump(mode="json"),
                    hybrid=dict(
                        schema_version=1, until_commit=failed.head, calls=[str(original["id"])]
                    ),
                )
            )
        ),
        "observation-search",
    )
    search = UUID(made["search_id"])
    suite = register_suite(research, repo, search, config, binding())
    actual = worker(research, ObservationOrderModel)
    source = drive(research, repo, search, actual, "screen")
    assert source["status"] == "needs_confirmation"
    assert source["trials"][-1]["result"]["verdict"] == "passed"
    study, reduced = reduce_and_open(research, repo, search, suite, actual)
    assert len(reduced["trials"]) == 1 and reduced["trials"][0]["result"]["verdict"] == "failed"
    assert reduced["field_reduction"]["summary"]["field_minimality"] == "no_applicable_fields"
    assert [a["operator"] for a in reduced["final_lock"]["lock"]["candidate"]["atoms"]] == [
        "TOOL_RESPONSE_PATCH"
    ]
    result = drive(research, repo, study, actual, "confirm")
    assert result["status"] == "verified_under_fixture", result
    assert (
        result["verification"]["confirm_passed"] == 6
        and result["verification"]["regression_passed"] == 4
    )
    assert (
        result["verification"]["has_observation_patch"]
        and not result["verification"]["production_eligible"]
    )
    for trial in result["trials"]:
        receipt = trial["result"]
        report = history.objects.get_json(receipt["replay_report_ref"], ObjectType.BLOB)
        if receipt["phase"] == "confirm":
            assert report["mode"] == "hybrid" and report["actual"]["synthetic_observations"] == 1
            assert report["observations"][0]["kind"] == "synthetic"
        else:
            assert (
                report["mode"] == "live"
                and trial["projection"]["observation_scope"] == "outside_source"
            )
        assert receipt["fresh_model_calls"] > 0 and receipt["test_only"]
    reviews = RepairReviewService(research)
    proposal = reviews.create(repo, study, actor="fixture", key="proposal")
    identity = UUID(proposal["proposal_id"])
    assert (
        proposal["proposal"]["after"] is None
        and proposal["proposal"]["adoption_scope"] == "save_experiment_only"
    )
    ticket = reviews.begin(repo, identity, actor="fixture", key="review")
    request = DecideRepairReview(
        review_id=ticket["review_id"],
        proposal_ref=proposal["proposal_ref"],
        expected_config_version=ticket["ticket"]["expected_config_version"],
        decision="approve",
    )
    before = count_configs(history)
    with pytest.raises(AgentGitError) as error:
        reviews.decide(repo, identity, request, actor="fixture", key="cannot-adopt")
    assert error.value.code == ErrorCode.FORBIDDEN and "观测" in error.value.message
    assert count_configs(history) == before
    # 直接伪造决定也不能绕过数据库的配置采纳约束。
    forged_id = uuid4()
    forged = RepairReviewDecision(
        proposal_id=str(identity),
        review_id=request.review_id,
        proposal_ref=request.proposal_ref,
        ticket_ref=ticket["ticket_ref"],
        expected_config_version=request.expected_config_version,
        decision="approve",
        actor="fixture",
        source_config_id=str(config),
        source_config_version=1,
        new_config_id=str(config),
        new_config_version=request.expected_config_version + 1,
        saved_experiment_id=None,
        lock_ref=proposal["proposal"]["verification"]["lock_ref"],
        verification_ref=proposal["proposal"]["verification_ref"],
    )
    obj = history.objects.put_json(ObjectType.BLOB, forged.model_dump(mode="json"))
    with (
        pytest.raises(DBAPIError, match="config adoption requires"),
        history.engine.begin() as connection,
    ):
        history._ref(connection, repo, obj, "repair_review_decision", str(forged_id), "decision")
        connection.execute(
            TABLES["proposal_reviews"]
            .insert()
            .values(
                id=forged_id,
                repository_id=repo,
                proposal_id=identity,
                review_id=UUID(request.review_id),
                decision="approve",
                decision_ref=obj.id,
                new_config_id=config,
                saved_experiment_id=None,
                actor="fixture",
                request_key="forged",
                request_digest="f" * 64,
            )
        )
    saved = reviews.decide(
        repo,
        identity,
        evolve(request, decision="save_experiment"),
        actor="fixture",
        key="save-experiment",
    )
    assert (
        saved["decision"]["execution"] == "not_started"
        and saved["decision"]["new_config_id"] is None
    )
    assert saved["saved_experiment"]["definition"]["patches"] == proposal["proposal"]["patches"]
    assert count_configs(history) == before and actual.run_once() is None
    capture_confirmation(
        history,
        result,
        source,
        reduced,
        case="S10-T05-real-observation-falsefix/v1",
        environment="AGENTGIT_S10_OBSERVATION_FALSEFIX_EVIDENCE",
    )
