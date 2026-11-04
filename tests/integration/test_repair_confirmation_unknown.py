"""实际模型返回未知结果后，确认和取消均不得重发或释放未知预留。"""

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_repair_confirmation import ConfirmationMock, adapter_binding, setup_confirmation

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import ModelRequest
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.services.jobs import JobService
from agentgit.services.repair_confirmation import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_actual_unknown_model_result_remains_reserved_through_restart_and_cancel(history, tmp_path):
    policy = "unknown-model-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    repo, study, research, _, _ = setup_confirmation(history, tmp_path, policy=policy)
    invocations = []

    class UnknownModel(ConfirmationMock):
        def invoke(self, payload):
            request = parse_contract(payload, ModelRequest)
            assert adapter_binding(policy) in request.state.goal.text
            invocations.append(payload)
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "模型请求已发送但结果尚不可确认")

    controller = RepairConfirmationWorker(research)
    dispatched = controller.run_once(repository=repo)
    trial = dispatched["trials"][0]
    worker = Worker(JobService(research), model_factory=UnknownModel)
    assert worker.run_once()["status"] == "outcome_unknown"
    for _ in range(2):
        waiting = RepairConfirmationWorker(research).run_once(repository=repo)
        assert waiting["status"] == "waiting_reconciliation"
        assert waiting["usage"]["active"] == 1 and len(waiting["trials"]) == 1
        assert waiting["verification"] is None and "result" not in waiting["trials"][0]
        assert waiting["trials"][0]["waiting"]["reason"] == "outcome_unknown"
        assert worker.run_once() is None
    controller.control.ledger.cancel(repo, study, actor="operator")
    for _ in range(2):
        cancelling = controller.run_once(repository=repo)
        assert cancelling["status"] == "cancelling" and cancelling["cancel_requested"]
        assert cancelling["usage"]["active"] == 1 and cancelling["verification"] is None
        assert len(cancelling["trials"]) == 1 and worker.run_once() is None
    assert len(invocations) == 1
    with history.engine.connect() as connection:
        calls = TABLES["external_calls"]
        rows = connection.execute(
            sa.select(calls.c.status, calls.c.result_ref).where(
                calls.c.run_id == UUID(trial["dispatch"]["initial_run_id"]),
                calls.c.provider.like("llm:%"),
            )
        ).all()
    assert rows == [("outcome_unknown", None)]
