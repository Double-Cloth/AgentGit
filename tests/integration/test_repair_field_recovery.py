"""预先冻结字段故障向量，检查未知、预算、安全与普通 Worker 恢复。"""

import hashlib
import time
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_repair_worker import ConditionalMock, create_case, drive

from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ModelRequest, Plan, PlanDecision, PlanNode, ToolDecision, evolve
from agentgit.repair.field_worker import RepairFieldDriver
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.services.jobs import JobService
from agentgit.services.repair_field_trials import RepairFieldTrialService
from agentgit.services.repair_fields import TABLES, RepairFieldService
from agentgit.services.repair_reductions import RepairReductionService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def binding(policy):
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return f"field_fault={policy};field_adapter_sha256={digest}"


class FieldFaultModel(ConditionalMock):
    """固定协议向量只验证工程边界，不用于随机效果或定位优势结论。"""

    def __init__(self, policy):
        self.policy = policy

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert binding(self.policy) in request.state.goal.text
        if self.policy == "unknown":
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "本次请求已经发送，响应尚不可确认")
        if self.policy == "budget":
            return encode(PlanDecision(plan=request.state.plan).model_dump(mode="json"))
        assert self.policy == "safety"
        plan = request.state.plan
        if request.phase == "plan":
            result = PlanDecision(
                plan=Plan(
                    nodes=(
                        *plan.nodes,
                        PlanNode(
                            key="forbidden",
                            kind="terminal_exec",
                            dependencies=tuple(n.key for n in plan.nodes),
                        ),
                    )
                )
            )
        elif not any(n.key == "forbidden" for n in plan.nodes):
            result = PlanDecision(plan=plan)
        elif any(n.key == "forbidden" for n in plan.ready(request.state.completed)):
            result = ToolDecision(
                node_key="forbidden", tool="terminal_exec", arguments={"schema_version": 1}
            )
        else:
            return super().invoke(payload)
        return encode(result.model_dump(mode="json"))


def pending(history, tmp_path, policy):
    repo, search, research, _ = create_case(
        history,
        tmp_path,
        adapter_binding=binding(policy),
        budget_transform=lambda b: (
            evolve(b, trial=evolve(b.trial, max_model_calls=2)) if policy == "budget" else b
        ),
    )
    assert drive(research, repo, search)["status"] == "needs_confirmation"
    ledger = RepairStudyService(history)
    study = UUID(
        ledger.submit(repo, NewRepairStudy(search_id=str(search)), actor="fixture", key="fields")[
            "study_id"
        ]
    )
    lease = ledger.claim("field-control", repository=repo)
    atomic = RepairReductionService(research)
    identity = UUID(ledger.reserve(lease)["trial_id"])
    atomic.dispatch(lease, identity)
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    assert atomic.settle(lease, identity)["settled"]
    fields = RepairFieldService(history)
    fields.initialize(lease)
    identity = UUID(fields.reserve(lease)["trial_id"])
    trials = RepairFieldTrialService(research)
    dispatched = trials.dispatch(lease, identity)
    return repo, study, research, ledger, lease, identity, trials, dispatched


def test_actual_unknown_remains_reserved_across_controller_restart_and_cancel(history, tmp_path):
    repo, study, research, ledger, lease, identity, trials, dispatched = pending(
        history, tmp_path, "unknown"
    )
    initial = ledger.view(repo, study)["field_reduction"]["trials"][0]["dispatch"]["initial_run_id"]
    ledger.release(lease)
    worker = Worker(JobService(research), model_factory=lambda: FieldFaultModel("unknown"))
    assert worker.run_once()["status"] == "outcome_unknown"
    for _ in range(2):
        view = RepairStudyWorker(research).run_once(repository=repo)
        assert view["status"] == "waiting_reconciliation" and view["usage"]["active"] == 1
        assert not view["final_lock"] and len(view["field_reduction"]["trials"]) == 1
        assert "result" not in view["field_reduction"]["trials"][0]
        assert worker.run_once() is None
    ledger.cancel(repo, study, actor="operator")
    for _ in range(2):
        view = RepairStudyWorker(research).run_once(repository=repo)
        assert view["status"] == "cancelling" and view["usage"]["active"] == 1
        assert worker.run_once() is None
    with history.engine.connect() as connection:
        calls = TABLES["external_calls"]
        rows = connection.execute(
            sa.select(calls.c.status, calls.c.result_ref).where(
                calls.c.run_id == UUID(initial), calls.c.provider.like("llm:%")
            )
        ).all()
    assert rows == [("outcome_unknown", None)]


@pytest.mark.parametrize("policy", ["budget", "safety"])
def test_actual_budget_or_safety_stops_without_accepting_incomplete_change(
    history, tmp_path, monkeypatch, policy
):
    repo, study, research, ledger, lease, identity, trials, _ = pending(history, tmp_path, policy)
    invoked = []
    original = ToolRegistry.invoke

    def invoke(self, name, *args, **kwargs):
        if name in ("terminal_exec", "code_exec"):
            invoked.append(name)
            pytest.fail("字段故障试验不得调用未授权执行器")
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(ToolRegistry, "invoke", invoke)
    actual = Worker(JobService(research), model_factory=lambda: FieldFaultModel(policy)).run_once()
    assert actual["status"] == ("budget_exhausted" if policy == "budget" else "failed")
    result = trials.settle(lease, identity)["result"]
    assert result["execution"]["category"] == policy
    assert result["execution"]["verdict"] == "incomplete" and result["fresh_model_calls"] > 0
    assert not invoked
    if policy == "budget":
        view = RepairFieldDriver(research).run_once(lease)
    else:
        view = ledger.view(repo, study)
    assert view["status"] == ("awaiting_samples" if policy == "budget" else "needs_attention")
    assert view["field_reduction"]["summary"]["field_minimality"] == "not_proven"
    assert view["field_reduction"]["summary"]["accepted_fields"] == 0
    assert bool(view["final_lock"]) == (policy == "budget")


def test_saved_model_response_recovers_with_own_family_and_no_duplicate_charge(history, tmp_path):
    repo, study, research, ledger, lease, identity, trials, _ = pending(
        history, tmp_path, "recovery"
    )
    initial = ledger.view(repo, study)["field_reduction"]["trials"][0]["dispatch"]["initial_run_id"]

    def fault(stage):
        if stage == "after_result":
            raise RuntimeError("字段模型响应已落盘，运行检查点尚未提交")

    with pytest.raises(RuntimeError):
        Worker(
            JobService(research, lease_ms=1000), model_factory=ConditionalMock, fault=fault
        ).run_once()
    time.sleep(1.15)
    recovered = Worker(JobService(research), model_factory=ConditionalMock).run_once()
    assert recovered["status"] == "completed" and recovered["run_id"] != initial
    result = trials.settle(lease, identity)["result"]
    assert result["execution"]["verdict"] == "passed"
    proof = history.objects.get_json(result["provenance_ref"], ObjectType.BLOB)
    assert proof["run_ids"] == [recovered["run_id"], initial]
    assert any(call["origin_call_id"] is not None for call in proof["model_calls"])
    assert (
        sum(call["origin_call_id"] is None for call in proof["model_calls"])
        == result["fresh_model_calls"]
    )
    assert result["fresh_model_calls"] == result["execution"]["usage"]["model_calls"]
    assert ledger.view(repo, study)["usage"]["active"] == 0
