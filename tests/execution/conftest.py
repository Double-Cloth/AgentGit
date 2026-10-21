"""生产执行服务使用真实持久 Job、租约、PostgreSQL 与 Docker。"""

import json
from pathlib import Path
from uuid import UUID

import pytest

from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.execution import ExecutionPolicy
from agentgit.core.models import EffectClass, EnvironmentManifest
from agentgit.core.research import ResearchGoal
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.executions import ExecutionService
from agentgit.services.history import TABLES, EventInput, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import LeaseHeartbeat


@pytest.fixture
def execution(history, tmp_path, image_id, engine):
    root = Path(__file__).resolve().parents[2]
    lock = json.loads((root / "sandbox/image.lock.json").read_text(encoding="utf-8"))
    research = ResearchService(history, tmp_path / "execution-data")
    repository = history.create_repository("真实容器耐久调用")
    config = research.create_config(repository)
    task = research.create_task(repository, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    submission = queue.enqueue(
        repository,
        task,
        actor="local-operator",
        key="execution-fixture",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    lease = queue.claim("execution-fixture")
    assert lease is not None
    managed = HistoryService(
        history.engine,
        history.objects,
        lease_guard=lambda connection, run_id: queue.guard(connection, lease, run_id),
    )
    environment = EnvironmentManifest(
        runtime_version="0.1.0",
        python_version="3.12",
        code_revision="execution-fixture",
        dependency_lock_digest="a" * 64,
        model_signature="no-model",
        prompt_digest="b" * 64,
        input_ref=history.register_input(repository, b"execution-fixture"),
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )
    first = managed.commit(
        repository,
        lease.run_id,
        expected_head=None,
        expected_version=0,
        state={"fixture": "before-execution"},
        environment=environment,
        events=(EventInput("fixture.ready", "execution", {}),),
        projection=lambda connection: connection.execute(
            TABLES["runs"]
            .update()
            .where(TABLES["runs"].c.id == lease.run_id)
            .values(status="running")
        ),
    )
    policy = ExecutionPolicy(image_id=image_id, packages_digest=lock["packages_sha256"])
    service = ExecutionService(managed, queue, lease, policy, engine_factory=lambda: engine)

    def gateway(service=service, after_result=None):
        def checkpoint():
            branch = managed.branch(repository)
            return branch["head_commit_id"], branch["version"]

        rules = tuple(
            ToolRule(
                provider=provider,
                signature_digest=service.signature(provider),
                effect=EffectClass.NON_IDEMPOTENT_WRITE,
                scope="sandbox",
                preauthorized=True,
            )
            for provider in ("tool:terminal_exec/v1", "tool:code_exec/v1")
        )
        return RecordedCallGateway(
            managed,
            repository,
            lease.run_id,
            checkpoint,
            authorization=ApprovalService(managed, ToolPolicy(rules=rules), actor="local-operator"),
            managed=service,
            after_result=after_result,
        )

    with LeaseHeartbeat(queue, lease) as heartbeat:
        yield {
            "history": managed,
            "queue": queue,
            "lease": lease,
            "repository": repository,
            "run": UUID(submission.run_id),
            "checkpoint": first.commit_id,
            "environment": environment,
            "policy": policy,
            "service": service,
            "gateway": gateway,
            "engine": engine,
            "heartbeat": heartbeat,
        }
