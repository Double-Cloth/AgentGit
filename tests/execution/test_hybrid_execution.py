"""Hybrid 只读观测之外仍执行真实容器，Recorded 再消费完整证据。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_agent_flow import submit

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import AgentState, BudgetLimits, BudgetUsage
from agentgit.core.versions import RecordedRange
from agentgit.replay.engine import RecordedReplayer
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.recordings import RecordingReader
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


def test_hybrid_observation_does_not_replace_real_execution_or_its_recorded_evidence(
    history, tmp_path, image_id, monkeypatch
):
    queue, repository, source = submit(history, tmp_path)
    assert Worker(queue).run_once()["status"] == "completed"
    manager = ManagementService(queue.research)
    commits = list(reversed(manager.commits(repository)["items"]))
    baseline = history.restore(repository, commits[3]["commit_id"])
    with history.engine.connect() as connection:
        selected = connection.execute(
            sa.select(TABLES["external_calls"].c.id).where(
                TABLES["external_calls"].c.run_id == UUID(source.run_id),
                TABLES["external_calls"].c.provider == "tool:source_read/v1",
                TABLES["external_calls"].c.logical_step_id == "read-0",
            )
        ).scalar_one()
    payload = queue.payload(queue.get(repository, UUID(source.job_id)))
    config = queue.research.config(repository, UUID(payload.config_id))
    goal = parse_contract(encode(baseline.data), AgentState).goal
    validator = ValidatorService(history).freeze(repository, goal, queue.research.corpus)
    original = history.branch(repository)
    submitted = manager.create_experiment(
        repository,
        NewExperiment(
            source_run_id=source.run_id,
            from_commit=commits[3]["commit_id"],
            validator_ref=validator,
            repeats=1,
            budget=BudgetLimits(),
            hybrid=HybridPolicy(until_commit=commits[-1]["commit_id"], calls=(str(selected),)),
            groups=(
                ExperimentGroup(key="A", label="原配置"),
                ExperimentGroup(key="B", label="核验引用", prompt=config.prompt + "核验所有引用。"),
            ),
        ),
        "hybrid-real-containers",
    )
    assert all(Worker(queue).run_once()["status"] == "completed" for _ in range(2))
    report = manager.experiment(repository, UUID(submitted["experiment_id"]))
    containers = set()
    for trial in report["trials"]:
        assert trial["verdict"] == "passed"
        actual = trial["replay"]["actual"]
        assert actual["model_calls"] == 13 and actual["tool_calls"] == 11
        assert actual["recorded_observations"] == 1
        with history.engine.connect() as connection:
            executions = list(
                connection.execute(
                    sa.select(TABLES["sandbox_executions"]).where(
                        TABLES["sandbox_executions"].c.run_id == UUID(trial["run_id"])
                    )
                ).mappings()
            )
            first = connection.execute(
                sa.select(TABLES["run_commits"].c.commit_id)
                .where(TABLES["run_commits"].c.run_id == UUID(trial["run_id"]))
                .order_by(TABLES["run_commits"].c.run_order)
                .limit(1)
            ).scalar_one()
        assert len(executions) == 2 and all(row["status"] == "complete" for row in executions)
        assert all(row["image_id"] == image_id and row["cleanup_confirmed"] for row in executions)
        containers.update(row["container_id"] for row in executions)
        view = manager.checkout(repository, first)
        environment = parse_contract(
            history.objects.get(view["manifest"]["environment_ref"], ObjectType.ENVIRONMENT),
            EnvironmentManifest,
        )
        bundle = RecordingReader(history).load(
            repository,
            RecordedRange(
                source_run_id=trial["run_id"],
                from_commit=first,
                until_commit=trial["checkpoint_commit_id"],
            ),
            expected_environment=environment,
        )
        trial_config = queue.research.config(
            repository, manager.jobs.run(repository, UUID(trial["run_id"]))["agent_config_id"]
        )
        budget_baseline = parse_contract(encode(trial["replay"]["budget_baseline"]), BudgetUsage)

        def forbidden(*args, **kwargs):
            pytest.fail("Hybrid 的 Recorded 不得再次构造容器或调用 Adapter/模拟器")

        with monkeypatch.context() as offline:
            for entry in (
                "socket.socket",
                "socket.create_connection",
                "subprocess.Popen",
                "agentgit.adapters.execution.engine.DockerEngine.__init__",
                "agentgit.adapters.execution.container.SandboxContainer.__init__",
                "agentgit.adapters.tools.ToolRegistry.invoke",
                "agentgit.adapters.mock.MockResearchModel.invoke",
                "agentgit.experiments.hybrid.HybridSelector.resolve",
            ):
                offline.setattr(entry, forbidden)
            replay = RecordedReplayer(history.objects.get).run(
                bundle, trial_config, queue.research.corpus, budget_baseline=budget_baseline
            )
        assert replay.actual_model_calls == replay.actual_tool_calls == 0
        assert replay.state.evaluation.passed
    assert len(containers) == 4
    assert history.branch(repository) == original
