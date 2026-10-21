"""真实执行服务的独立进程故障助手，仅允许隔离测试数据库 Schema。"""

import json
import re
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from agentgit.config import Settings, database_url
from agentgit.core.canonical import encode
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.execution import ExecutionPolicy
from agentgit.core.jobs import Lease
from agentgit.core.models import EffectClass, parse_contract
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.executions import ExecutionService
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore


def main():
    schema, root, objects, input_path = sys.argv[1:]
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    engine = create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    bound = data["lease"]
    lease = Lease(
        UUID(bound["job_id"]),
        UUID(bound["repository_id"]),
        UUID(bound["run_id"]),
        bound["worker"],
        bound["fencing_token"],
        bound["lease_ms"],
    )
    base = HistoryService(engine, ObjectStore(Path(objects)))
    queue = JobService(ResearchService(base, Path(root)))
    history = HistoryService(
        engine,
        base.objects,
        lease_guard=lambda connection, run_id: queue.guard(connection, lease, run_id),
    )
    policy = parse_contract(encode(data["policy"]), ExecutionPolicy)
    service = ExecutionService(history, queue, lease, policy)
    provider = "tool:terminal_exec/v1"
    rule = ToolRule(
        provider=provider,
        signature_digest=service.signature(provider),
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        scope="sandbox",
        preauthorized=True,
    )

    def checkpoint():
        branch = history.branch(lease.repository_id)
        return branch["head_commit_id"], branch["version"]

    def forbidden():
        raise AssertionError("不能执行宿主回退")

    gateway = RecordedCallGateway(
        history,
        lease.repository_id,
        lease.run_id,
        checkpoint,
        authorization=ApprovalService(history, ToolPolicy(rules=(rule,)), actor="local-operator"),
        managed=service,
    )
    gateway.call(
        step="execute",
        attempt=1,
        provider=provider,
        signature=service.signature(provider),
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request=encode(data["arguments"]),
        invoke=forbidden,
        max_bytes=16384,
    )


if __name__ == "__main__":
    main()
