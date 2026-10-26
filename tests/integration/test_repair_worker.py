"""真实数据库和独立研究 Worker 的自动搜索，不读取 S8 标签或修复评估保留集。"""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from diagnosis_evidence import pack_repository_evidence

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, RunStatus, parse_contract
from agentgit.core.repair import PhaseQuotas, RepairBinding
from agentgit.core.research import (
    AgentConfig,
    ModelRequest,
    PlanDecision,
    ResearchGoal,
    ToolDecision,
    evolve,
    parse_decision,
)
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.policy import mock_budget, mock_policy_version
from agentgit.repair.space import freeze_space
from agentgit.repair.templates import TemplateAnchor, model_templates
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.interventions import InterventionService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService, public
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


class ConditionalMock(MockResearchModel):
    """固定协议的受控故障替身；适配器字节摘要显式冻结在测试任务中。"""

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() in request.state.goal.text
        mode = request.state.goal.text.split(";", 1)[0]
        result = parse_decision(super().invoke(payload))
        if mode == "joint" and isinstance(result, PlanDecision):
            result = evolve(
                result,
                plan=evolve(
                    result.plan,
                    nodes=tuple(n for n in result.plan.nodes if n.kind != "write_sources"),
                ),
            )
        if isinstance(result, ToolDecision) and result.arguments.get("path") == "sources.json":
            has_rule = "仅引用本次已读取" in request.prompt
            if mode == "execution" and has_rule:
                raise AgentGitError(ErrorCode.TOOL_EMPTY, "固定非确定执行失败，不能算作硬验收无解")
            if mode == "never" or not has_rule:
                result = evolve(
                    result, arguments={**result.arguments, "path": "fixture-sources.json"}
                )
        return encode(result.model_dump(mode="json"))


def create_case(
    history,
    tmp_path,
    *,
    mode="prompt",
    budget_transform=lambda b: b,
    adapter_binding=None,
    fixture_scope=None,
):
    research = ResearchService(history, tmp_path / "research")
    repo = history.create_repository("自动搜索受控 Mock")
    config = research.create_config(repo)
    adapter_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    extra = f"{adapter_binding};" if adapter_binding else ""
    goal = ResearchGoal(text=f"{mode};adapter_sha256={adapter_hash};{extra}比较三个合成框架")
    task = research.create_task(repo, config, goal)
    session = research.start(repo, task)
    assert session.execute(ConditionalMock()).status == RunStatus.FAILED
    validator = ValidatorService(history).freeze(repo, goal, research.corpus)
    node_kind = "write_report" if mode == "joint" else "write_sources"
    with history.engine.connect() as connection:
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == session.run_id,
                )
            ).mappings()
        )
        for call in calls:
            intent = parse_contract(
                history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
            )
            if not intent.occurrence.provider.startswith("llm:"):
                continue
            request = parse_contract(
                history.objects.get(intent.request_ref, ObjectType.BLOB), ModelRequest
            )
            ready = request.state.plan.ready(request.state.completed) if request.state.plan else ()
            if request.phase == "act" and ready and ready[0].kind == node_kind:
                event = connection.execute(
                    sa.select(TABLES["events"].c.id).where(
                        TABLES["events"].c.run_id == session.run_id,
                        TABLES["events"].c.logical_step_id == call["logical_step_id"],
                        TABLES["events"].c.type == "llm.requested",
                    )
                ).scalar_one()
                base = intent.checkpoint
                break
        else:
            pytest.fail("没有找到预定的模型决策锚点")
    context = InterventionService(history).context(repo, session.run_id, base, event, validator)
    view = VersionService(history).checkout(repo, base)
    anchor = TemplateAnchor.model_validate_json(
        encode(
            {
                key: context[key]
                for key in (
                    "source_run_id",
                    "base_commit",
                    "target_commit_id",
                    "target_event_id",
                    "precondition",
                )
            }
        )
    )
    config = AgentConfig.model_validate_json(encode(context["config"]))
    generated = model_templates(
        anchor,
        config,
        StateSnapshot.model_validate_json(encode(view["snapshot"])),
        history.objects.get,
    )
    budget = budget_transform(mock_budget(config))
    binding = RepairBinding(
        repository_id=str(repo),
        failed_run_id=str(session.run_id),
        base_commit=base,
        environment_ref=view["manifest"]["environment_ref"],
        validator_ref=validator,
        policy_version=mock_policy_version(budget),
    )
    space = freeze_space(binding, generated.patches, source_reason="完整有限模板；不使用人工标签")
    manager = ManagementService(research)
    identities = tuple(
        manager.create_intervention(repo, p.intervention, f"template-{index}")["intervention_id"]
        for index, p in enumerate(space.patches)
    )
    submitted = RepairSearchService(history).submit(
        space,
        budget,
        identities,
        actor="test-operator",
        key="automatic-search",
        fixture_scope=fixture_scope,
    )
    return repo, UUID(submitted["search_id"]), research, session


def drive(research, repo, search):
    runner = RepairSearchWorker(research)
    worker = Worker(JobService(research), model_factory=ConditionalMock)
    for _ in range(70):
        result = runner.run_once(repository=repo)
        assert result is not None
        if result["status"] in (
            "needs_confirmation",
            "budget_exhausted",
            "no_solution_in_space",
            "not_found",
            "needs_attention",
            "cancelled",
        ):
            assert worker.run_once() is None
            return result
        worker.run_once()
    pytest.fail("有界搜索未终止")


def archive(history, result, session, mode):
    folder = os.environ.get("AGENTGIT_S9_SEARCH_EVIDENCE")
    if not folder:
        return
    with history.engine.connect() as connection:
        indexes = {
            name: [
                public(dict(row)) for row in connection.execute(sa.select(TABLES[name])).mappings()
            ]
            for name in (
                "repair_searches",
                "repair_jobs",
                "repair_proposals",
                "repair_trials",
                "repair_trial_jobs",
                "repair_trial_results",
                "repair_trial_waits",
                "repair_events",
                "jobs",
                "replay_requests",
            )
        }
    evidence = {
        "repository_id": str(session.repository_id),
        "failed_run_id": str(session.run_id),
        "case_id": f"S9-automatic-search/{mode}/v1",
        "reference_run_id": str(session.run_id),
        "reference_role": "source_alias_for_archive_only",
        "graph": BehaviorService(history)
        .trace(session.repository_id, session.run_id)
        .model_dump(mode="json"),
        "reference_graph": BehaviorService(history)
        .trace(session.repository_id, session.run_id)
        .model_dump(mode="json"),
    }
    bundle = pack_repository_evidence(history, evidence)
    path = Path(folder) / f"{mode}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            dict(
                schema_version=1,
                test_only=True,
                search=result,
                indexes=indexes,
                adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                evidence=evidence,
                raw_repository=bundle,
            ),
            handle,
            ensure_ascii=False,
            indent=2,
        )


@pytest.mark.parametrize(
    "mode,expected,explored",
    [
        ("prompt", "needs_confirmation", 1),
        ("joint", "needs_confirmation", 4),
        ("never", "no_solution_in_space", 2),
        ("execution", "not_found", 2),
    ],
)
def test_automatic_prompt_joint_and_scoped_failure(history, tmp_path, mode, expected, explored):
    repo, search, research, session = create_case(history, tmp_path, mode=mode)
    original = history.branch(repo)
    result = drive(research, repo, search)
    assert result["status"] == expected
    decision = result["decision"]
    assert decision["explored_proposals"] == explored
    assert decision["confirmation"] == "not_run" and decision["minimality"] == "not_tested"
    assert not decision["global_optimum"] and decision["test_only"]
    assert decision["usage"]["active"] == 0
    assert result["trials"][0]["result"]["verdict"] == "chain_valid"
    assert result["trials"][0]["result"]["usage"]["tokens"] == 0
    if mode == "joint":
        assert all(t["result"]["verdict"] == "failed" for t in result["trials"][1:4])
        assert decision["unexplored_proposals"] == 3
    if expected == "needs_confirmation":
        assert decision["selected_change_bytes"] > 0 and decision["passed_proposals"] == 1
    if mode == "execution":
        assert decision["determinate_failures"] == 1
    assert history.branch(repo) == original
    assert RepairSearchWorker(research).run_once(repository=repo) is None
    archive(history, result, session, mode)


def test_budget_stop_does_not_claim_unexplored_space_is_unsatisfiable(history, tmp_path):
    repo, search, research, session = create_case(
        history,
        tmp_path,
        mode="never",
        budget_transform=lambda b: evolve(
            b,
            quotas=PhaseQuotas(screen=2, reduce=6, confirm=6, regression=4),
        ),
    )
    result = drive(research, repo, search)
    assert result["status"] == "budget_exhausted"
    assert result["decision"]["unexplored_proposals"] == 1
    assert result["decision"]["determinate_failures"] == 1
    assert result["stop_reason"] == "phase_trials"
    archive(history, result, session, "budget")


def test_controller_restart_reuses_dispatched_job_and_preserves_unknown_reservation(
    history, tmp_path, monkeypatch
):
    repo, search, research, _ = create_case(history, tmp_path)

    def lost_ack(stage):
        if stage == "after_dispatch_commit":
            raise RuntimeError("模拟控制器丢失提交响应")

    with pytest.raises(RuntimeError):
        RepairSearchWorker(research, fault=lost_ack).run_once(repository=repo)
    ledger = RepairJobService(history)
    first = ledger.view(repo, search)["trials"][0]
    runner = RepairSearchWorker(research)
    assert runner.run_once(repository=repo)["trials"][0] == first
    worker = Worker(JobService(research), model_factory=ConditionalMock)
    assert worker.run_once()["status"] == "completed"
    assert len(runner.run_once(repository=repo)["trials"]) == 2
    assert worker.run_once()["status"] == "completed"
    with monkeypatch.context() as unresolved:
        unresolved.setattr(runner.trials, "_usage", lambda report: None)
        waiting = runner.run_once(repository=repo)
        assert waiting["status"] == "waiting_reconciliation"
        assert waiting["usage"]["active"] == 1
        assert len(runner.run_once(repository=repo)["trials"]) == 2
    result = runner.run_once(repository=repo)
    assert result["status"] == "needs_confirmation"
    assert result["trials"][0]["dispatch"] == first["dispatch"]


def test_cancel_queued_trial_stops_expansion_and_waits_for_research_worker(history, tmp_path):
    repo, search, research, _ = create_case(history, tmp_path)
    runner = RepairSearchWorker(research)
    initial = runner.run_once(repository=repo)
    RepairJobService(history).cancel(repo, search, actor="operator")
    cancelling = runner.run_once(repository=repo)
    assert cancelling["cancel_requested"] and cancelling["usage"]["active"] == 1
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    result = runner.run_once(repository=repo)
    assert result["status"] == "cancelled" and result["usage"]["active"] == 0
    assert len(result["trials"]) == len(initial["trials"]) == 1
