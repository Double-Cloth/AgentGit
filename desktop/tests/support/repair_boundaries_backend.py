"""真实修复边界：预先冻结确认阶段的未知故障，只控制自己的测试 Worker。"""

import hashlib
import json
import sys
import time
from pathlib import Path

import repair_backend as base
import sqlalchemy as sa
from test_repair_confirmation import ConfirmationMock, adapter_binding

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import ModelRequest


def policy():
    return (
        "desktop-first-confirm-unknown/v1;"
        "base-harness-sha256="
        + hashlib.sha256(Path(base.__file__).read_bytes()).hexdigest()
        + ";edge-harness-sha256="
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )


def seed(history, root):
    return base.seed(history, root, mode="prompt", frozen_policy=policy())


class UnknownConfirmationModel(ConfirmationMock):
    def __init__(self, root):
        super().__init__(policy())
        self.root = root

    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert adapter_binding(policy()) in request.state.goal.text
        with (self.root / "unknown-invocations.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(dict(request_sha256=hashlib.sha256(payload).hexdigest())) + "\n"
            )
        raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "固定边界注入：模型请求已发送，结果未知")


def worker_main(schema, root):
    history = base.history_for(schema, root)
    research = base.ResearchService(history, root / "seed" / "research")

    def factory():
        tables = base.TABLES
        with history.engine.connect() as connection:
            row = connection.execute(
                sa.select(tables["repair_validation_trials"].c.ordinal)
                .join(
                    tables["repair_validation_jobs"],
                    tables["repair_validation_jobs"].c.id
                    == tables["repair_validation_trials"].c.id,
                )
                .join(
                    tables["jobs"], tables["jobs"].c.id == tables["repair_validation_jobs"].c.job_id
                )
                .where(tables["jobs"].c.status == "running")
            ).scalar_one_or_none()
        return UnknownConfirmationModel(root) if row == 0 else ConfirmationMock(policy())

    worker = base.Worker(base.JobService(research), model_factory=factory)
    controllers = [
        base.RepairSearchWorker(research),
        base.RepairStudyWorker(research),
        base.RepairConfirmationWorker(research),
    ]
    (root / "worker-ready").write_text("ready")
    try:
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists():
                (root / "worker-gated").write_text("gated")
                time.sleep(0.03)
                continue
            (root / "worker-gated").unlink(missing_ok=True)
            for controller in controllers:
                controller.run_once()
            worker.run_once()
            time.sleep(0.03)
    finally:
        history.engine.dispose()


def extra_command(name, request, history, root, seeded, worker):
    assert seeded is not None and worker.poll() is None
    if name == "worker_gate":
        gate = root / "worker-gate"
        if request["enabled"]:
            gate.write_text("controlled")
            deadline = time.monotonic() + 15
            while not (root / "worker-gated").exists():
                assert worker.poll() is None and time.monotonic() < deadline
                time.sleep(0.03)
        else:
            gate.unlink(missing_ok=True)
        return dict(enabled=request["enabled"])
    assert name == "edge_audit", "未知修复边界命令"
    with history.engine.connect() as connection:
        indexes = {
            name: [dict(row) for row in connection.execute(sa.select(base.TABLES[name])).mappings()]
            for name in (
                "jobs",
                "external_calls",
                "repair_trials",
                "repair_validation_trials",
                "repair_validation_jobs",
                "repair_validation_waits",
            )
        }
    path = root / "unknown-invocations.jsonl"
    invocations = (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )
    # 归档保留 UUID/时间的字符串表示，不修改数据库或未知状态。
    return json.loads(json.dumps(dict(indexes=indexes, invocations=invocations), default=str))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        base.main(seed_case=seed, extra_command=extra_command)
