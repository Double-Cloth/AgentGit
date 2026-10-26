"""新任务的计划模板走同一 Runtime；暂停和检查点恢复不重复规划或继承状态。"""

import hashlib
import time
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters.mock import MockResearchModel, research_plan
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, ModelRequest, ResearchGoal
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


class TemplateOnlyModel(MockResearchModel):
    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() in request.state.goal.text
        assert request.phase == "act", "配置模板不应再调用模型生成首次计划"
        return super().invoke(payload)


@pytest.mark.parametrize("boundary", [None, 1, 2, "pause"])
def test_durable_new_task_seeds_template_once_across_restart_or_pause(history, tmp_path, boundary):
    research = ResearchService(history, tmp_path / "template-jobs")
    repo = history.create_repository("配置计划模板实际任务")
    plan = research_plan()
    config = research.create_config(repo, AgentConfig(plan_template=plan))
    goal = ResearchGoal(
        text="template-adapter-sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )
    task = research.create_task(repo, config, goal)
    queue = JobService(research)
    made = queue.enqueue(
        repo,
        task,
        actor="fixture",
        key="new-template-task",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    initial = UUID(made.run_id)
    checkpoints = []

    def fault(point):
        if point == "after_checkpoint":
            checkpoints.append(point)
            if len(checkpoints) == boundary:
                raise RuntimeError("模板初始化检查点已提交后中断")

    if boundary in (1, 2):
        with pytest.raises(RuntimeError):
            Worker(
                JobService(research, lease_ms=1000), model_factory=TemplateOnlyModel, fault=fault
            ).run_once()
        time.sleep(1.15)
    elif boundary == "pause":
        queue.request_control(repo, initial, "pause", expected_version=0)
        assert Worker(queue, model_factory=TemplateOnlyModel).run_once()["status"] == "paused"
        state = queue.run(repo, initial)
        queue.request_control(repo, initial, "resume", expected_version=state["state_version"])
    result = Worker(queue, model_factory=TemplateOnlyModel).run_once()
    assert result["status"] == "completed", result
    job = queue.get(repo, UUID(made.job_id))
    restored = history.restore(repo, job["checkpoint_commit_id"])
    assert (
        restored.data["plan"] == plan.model_dump(mode="json") and restored.data["plan_version"] == 1
    )
    assert restored.data["usage"]["replans"] == 0
    with history.engine.connect() as connection:
        nodes = (
            connection.execute(
                sa.select(TABLES["task_nodes"]).where(TABLES["task_nodes"].c.task_id == task)
            )
            .mappings()
            .all()
        )
        assert len(nodes) == len(plan.nodes) and {n["plan_version"] for n in nodes} == {1}
        events = TABLES["events"]
        accepted = connection.execute(
            sa.select(events.c.id)
            .join(TABLES["runs"], TABLES["runs"].c.id == events.c.run_id)
            .where(TABLES["runs"].c.task_id == task, events.c.type == "plan.accepted")
        ).all()
        assert len(accepted) == 1
