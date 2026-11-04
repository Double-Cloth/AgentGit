"""S7：真实 PostgreSQL 与现有 Worker 的 A/B 独立运行、恢复和原子排队。"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_recorded_replayer import replay_source as replay_source

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, FreezeValidator, NewExperiment
from agentgit.core.management import ControlRun
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, BudgetLimits, Plan, PlanNode, ResearchGoal, evolve
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def prepared(replay_source, *, repeats=1, budget=None, groups=None):
    session, _, commits, _ = replay_source
    validators = ValidatorService(session.history)
    ref = validators.freeze(session.repository_id, session.state.goal, session.service.corpus)
    return ManagementService(session.service), NewExperiment(
        source_run_id=str(session.run_id),
        from_commit=commits[15],
        validator_ref=ref,
        repeats=repeats,
        budget=budget or BudgetLimits(),
        groups=groups
        or (
            ExperimentGroup(key="A", label="原 Prompt"),
            ExperimentGroup(
                key="B", label="增加核验提醒", prompt=session.config.prompt + "核验所有引用。"
            ),
        ),
    )


def run_all(manager, count):
    worker = Worker(manager.jobs)
    results = [worker.run_once() for _ in range(count)]
    assert all(item is not None for item in results)
    assert worker.run_once() is None
    return results


def test_ab_five_trials_have_independent_runs_inputs_files_and_actual_usage(
    history,
    replay_source,
):
    session, final, commits, _ = replay_source
    manager, request = prepared(replay_source, repeats=5)
    original = history.branch(session.repository_id)
    source_files = session.workspace.contents()
    result = manager.create_experiment(session.repository_id, request, "ab-five")
    assert manager.create_experiment(session.repository_id, request, "ab-five") == result
    assert result["trial_count"] == 10
    assert len({item["run_id"] for item in result["trials"]}) == 10
    assert len({item["job_id"] for item in result["trials"]}) == 10
    experiment_id = UUID(result["experiment_id"])
    before = manager.experiment(session.repository_id, experiment_id)
    assert all(item["verdict"] == "incomplete" for item in before["trials"])
    assert all(group["passed"] == 0 for group in before["groups"])
    assert all(item["status"] == "completed" for item in run_all(manager, 10))
    report = manager.experiment(session.repository_id, experiment_id)
    assert report["groups"] == [
        {"key": key, "total": 5, "passed": 5, "incomplete": 0} for key in ("A", "B")
    ]
    for group in report["statistics"]["groups"]:
        assert group["success_rate"] == {"numerator": "1", "denominator": "1"}
        assert group["hard_assertions"]["passed"] == 5
        metrics = group["assessed_trials"]["metrics"]
        assert metrics["actual.model_calls"]["sum"] == "15"
        assert metrics["actual.model_calls"]["sample_variance"] == {
            "numerator": "0",
            "denominator": "1",
        }
        assert metrics["mock.total_bytes"]["samples"] == 5
        assert metrics["provider.input_tokens"]["sum"] == "0"
    states = []
    workspace_paths = set()
    input_refs = {"A": set(), "B": set()}
    validator = manager.experiments.validators.manifest(
        session.repository_id, request.validator_ref
    )
    for trial in report["trials"]:
        job = manager.jobs.get(session.repository_id, UUID(trial["job_id"]))
        run = manager.jobs.run(session.repository_id, UUID(trial["run_id"]))
        assert run["base_commit_id"] == request.from_commit
        assert run["origin_run_id"] == session.run_id
        assert trial["assessment"]["validator_ref"] == request.validator_ref
        assert (
            trial["replay"]["budget_baseline"] == before["trials"][0]["replay"]["budget_baseline"]
        )
        assert trial["replay"]["actual"]["model_calls"] == 3
        assert trial["replay"]["actual"]["tool_calls"] == 2
        assert trial["replay"]["actual"]["unknown_outcomes"] == 0
        payload = manager.jobs.payload(job)
        assert payload.limits == request.budget
        snapshot = history.restore(session.repository_id, job["checkpoint_commit_id"])
        states.append(parse_contract(encode(snapshot.data), AgentState))
        area = (
            session.service.data_root
            / "workspaces"
            / trial["run_id"]
            / f"lease-{job['fencing_token']}"
        )
        assert area.is_dir()
        workspace_paths.add(area)
        config = session.service.config(session.repository_id, run["agent_config_id"])
        assert config.tools == session.config.tools and config.execution_policy is None
        environment = manager.checkout(session.repository_id, job["checkpoint_commit_id"])[
            "environment"
        ]
        assert environment["code_revision"] == validator.code_revision
        assert environment["dependency_lock_digest"] == validator.dependency_lock_digest
        assert environment["evaluation_time"] == validator.evaluation_time
        assert environment["random_seed"] == 0
        assert environment["prompt_digest"] == hashlib.sha256(config.prompt.encode()).hexdigest()
        input_refs[trial["group"]].add(environment["input_ref"])
        if trial["group"] == "B":
            assert config.prompt == request.groups[1].prompt
        else:
            assert config.prompt == session.config.prompt
        calls = TABLES["external_calls"]
        with history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(calls)
                    .where(
                        calls.c.run_id == UUID(trial["run_id"]),
                        calls.c.origin_call_id.is_not(None),
                    )
                ).scalar_one()
                == 0
            )
    assert len(workspace_paths) == 10
    assert len(input_refs["A"]) == len(input_refs["B"]) == 1
    assert input_refs["A"] != input_refs["B"]
    assert all(state.evaluation.passed for state in states)
    first_path, second_path = list(workspace_paths)[:2]
    (first_path / "report.md").write_bytes("仅修改一个 trial 的测试工作区".encode())
    assert (second_path / "report.md").read_bytes() == source_files["report.md"]
    assert history.branch(session.repository_id) == original
    assert history.restore(session.repository_id, commits[-1]).data == final.model_dump(mode="json")
    assert session.workspace.contents() == source_files
    assert manager.experiments.manifest(session.repository_id, experiment_id).request == request


@pytest.mark.parametrize("action", ["budget", "cancel", "pause"])
def test_unfinished_trials_are_not_passed_and_paused_job_resumes(history, replay_source, action):
    session, *_ = replay_source
    budget = BudgetLimits(max_model_calls=1) if action == "budget" else BudgetLimits()
    manager, request = prepared(replay_source, budget=budget)
    result = manager.create_experiment(session.repository_id, request, "control-" + action)
    selected = result["trials"][0]
    run_id = UUID(selected["run_id"])
    if action != "budget":
        run = manager.jobs.run(session.repository_id, run_id)
        manager.control(
            session.repository_id,
            run_id,
            action,
            ControlRun(expected_version=run["state_version"]),
            "control-" + action,
        )
    run_all(manager, 2)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    first = report["trials"][0]
    assert (
        first["status"]
        == {"budget": "budget_exhausted", "cancel": "cancelled", "pause": "paused"}[action]
    )
    assert first["verdict"] == "incomplete"
    assert report["groups"][0]["passed"] == 0
    if action == "pause":
        run = manager.jobs.run(session.repository_id, run_id)
        manager.control(
            session.repository_id,
            run_id,
            "resume",
            ControlRun(expected_version=run["state_version"]),
            "resume-trial",
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        resumed = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
        assert resumed["trials"][0]["verdict"] == "passed"


def test_worker_crash_keeps_trial_identity_and_reports_current_recovery_run(history, replay_source):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    result = manager.create_experiment(session.repository_id, request, "recover-trial")
    crashed = False

    def crash(point):
        nonlocal crashed
        if point == "after_result" and not crashed:
            crashed = True
            raise RuntimeError("实验调用结果已落盘后中断")

    with pytest.raises(RuntimeError, match="实验调用结果"):
        Worker(manager.jobs, fault=crash).run_once()
    # 同事务创建的 Job 按 UUID 打破时间并列，不能假设 A0 必然最先领取。
    with history.engine.connect() as connection:
        job_id = connection.execute(
            sa.select(TABLES["jobs"].c.id).where(TABLES["jobs"].c.status == "running")
        ).scalar_one()
    first = next(item for item in result["trials"] if item["job_id"] == str(job_id))
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    assert (
        next(item for item in report["trials"] if item["job_id"] == str(job_id))["verdict"]
        == "incomplete"
    )
    pending = next(item for item in report["trials"] if item["job_id"] == str(job_id))
    assert pending["replay"]["actual"]["model_usage"]["mock"]["total_bytes"] > 0
    assert pending["replay"]["new_usage"]["tokens"] == 0
    before = manager.jobs.get(session.repository_id, job_id)
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    run_all(manager, 2)
    after = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    recovered = next(item for item in after["trials"] if item["job_id"] == str(job_id))
    assert recovered["initial_run_id"] == first["run_id"]
    assert recovered["run_id"] != first["run_id"]
    assert recovered["job_id"] == first["job_id"]
    assert recovered["verdict"] == "passed"
    assert manager.jobs.get(session.repository_id, job_id)["deadline_at"] == before["deadline_at"]
    assert recovered["replay"]["actual"]["model_calls"] == 3
    assert recovered["replay"]["actual"]["model_usage"]["mock"]["calls"] == 3


def test_partial_enqueue_rolls_back_every_trial_and_can_retry_same_key(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    original = manager.experiments.replays.submit
    attempts = 0

    def fail_second(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("第二个 trial 排队失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager.experiments.replays, "submit", fail_second)
    with pytest.raises(RuntimeError, match="第二个 trial"):
        manager.create_experiment(session.repository_id, request, "atomic-experiment")
    with history.engine.connect() as connection:
        for name in ("experiments", "experiment_trials", "jobs", "replay_requests", "branch_forks"):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                == 0
            )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["branches"])
            ).scalar_one()
            == 1
        )
    monkeypatch.setattr(manager.experiments.replays, "submit", original)
    assert (
        manager.create_experiment(session.repository_id, request, "atomic-experiment")[
            "trial_count"
        ]
        == 2
    )


def test_cross_project_bad_input_and_escalated_budget_are_rejected(history, replay_source):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    other = history.create_repository("其他实验项目")
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(other, request, "wrong-project")
    assert caught.value.code == ErrorCode.NOT_FOUND
    wrong_validator = ValidatorService(history).freeze(
        session.repository_id, ResearchGoal(text="其他任务"), session.service.corpus
    )
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(
            session.repository_id, evolve(request, validator_ref=wrong_validator), "wrong-input"
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(
            session.repository_id,
            evolve(request, budget=BudgetLimits(max_tokens=1_000_001)),
            "bad-budget",
        )
    assert caught.value.code == ErrorCode.FORBIDDEN
    result = manager.create_experiment(session.repository_id, request, "owned-experiment")
    with pytest.raises(AgentGitError) as caught:
        manager.experiment(other, UUID(result["experiment_id"]))
    assert caught.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(
            session.repository_id, evolve(request, repeats=2), "owned-experiment"
        )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT


def test_plan_cannot_change_completed_nodes_and_manifest_indices_are_immutable(
    history, replay_source
):
    session, *_ = replay_source
    manager, request = prepared(
        replay_source,
        groups=(
            ExperimentGroup(key="A", label="基线"),
            ExperimentGroup(
                key="B", label="非法计划", plan=Plan(nodes=(PlanNode(key="other", kind="search"),))
            ),
        ),
    )
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(session.repository_id, request, "bad-plan")
    assert caught.value.code == ErrorCode.PLAN_INVALID
    manager, request = prepared(replay_source)
    manager.create_experiment(session.repository_id, request, "immutable-experiment")
    for table_name in ("experiments", "experiment_trials"):
        table = TABLES[table_name]
        with (
            history.engine.begin() as connection,
            pytest.raises(DBAPIError),
            connection.begin_nested(),
        ):
            connection.execute(table.delete())


def test_remaining_plan_change_is_frozen_applied_and_independently_validated(
    history, replay_source
):
    session, *_ = replay_source
    original = session.state.plan
    replacement = evolve(
        original,
        nodes=tuple(
            evolve(node, key="write-report-verified")
            if node.key == "write-report"
            else evolve(node, dependencies=("write-report-verified",))
            if node.key == "write-sources"
            else node
            for node in original.nodes
        ),
    )
    manager, request = prepared(
        replay_source,
        groups=(
            ExperimentGroup(key="A", label="原计划"),
            ExperimentGroup(key="B", label="更换未执行节点", plan=replacement),
        ),
    )
    result = manager.create_experiment(session.repository_id, request, "plan-condition")
    run_all(manager, 2)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    assert all(trial["verdict"] == "passed" for trial in report["trials"])
    states = [
        parse_contract(
            encode(history.restore(session.repository_id, trial["checkpoint_commit_id"]).data),
            AgentState,
        )
        for trial in report["trials"]
    ]
    assert states[0].plan == original
    assert states[1].plan == replacement
    assert states[1].plan_version == states[0].plan_version + 1
    assert "write-report-verified" in states[1].completed
    assert "write-report" not in states[1].completed
    assert (
        manager.experiments.manifest(session.repository_id, UUID(result["experiment_id"]))
        .request.groups[1]
        .plan
        == replacement
    )


def test_concurrent_same_key_schedules_one_atomic_experiment(history, replay_source):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: manager.create_experiment(
                    session.repository_id, request, "concurrent-experiment"
                ),
                range(2),
            )
        )
    assert results[0] == results[1]
    with history.engine.connect() as connection:
        for name, expected in (("experiments", 1), ("experiment_trials", 2), ("jobs", 2)):
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                == expected
            )


def test_trial_group_cannot_be_swapped_in_database(history, replay_source):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    result = manager.create_experiment(session.repository_id, request, "guard-group")
    trials = TABLES["experiment_trials"]
    with (
        history.engine.begin() as connection,
        pytest.raises(DBAPIError, match="trial must own a new queued job"),
        connection.begin_nested(),
    ):
        row = dict(
            connection.execute(
                sa.select(trials).where(
                    trials.c.experiment_id == UUID(result["experiment_id"]),
                    trials.c.group_key == "A",
                )
            )
            .mappings()
            .one()
        )
        connection.execute(trials.insert().values(**{**row, "group_key": "B"}))


def test_reporting_retries_when_worker_finishes_between_status_reads(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    result = manager.create_experiment(session.repository_id, request, "race-report")
    selected = UUID(result["trials"][0]["job_id"])
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id != selected)
            .values(available_at=sa.func.clock_timestamp() + sa.text("interval '1 hour'"))
        )
    original = manager.experiments.replays.report
    completed = False

    def advance(repository, job_id):
        nonlocal completed
        if job_id == selected and not completed:
            completed = True
            assert Worker(manager.jobs).run_once()["status"] == "completed"
        return original(repository, job_id)

    monkeypatch.setattr(manager.experiments.replays, "report", advance)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    assert report["trials"][0]["status"] == "completed"
    assert report["trials"][0]["verdict"] == "passed"
    assert report["trials"][1]["status"] == "queued"
    assert report["trials"][1]["verdict"] == "incomplete"


def test_repeated_fixture_experiments_reproduce_logical_and_actual_call_metrics(
    history, replay_source
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    measurements = []
    statistical_metrics = []
    for index in range(2):
        result = manager.create_experiment(session.repository_id, request, f"repeat-{index}")
        run_all(manager, 2)
        report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
        statistical_metrics.append(
            [
                {
                    name: value
                    for name, value in group["assessed_trials"]["metrics"].items()
                    if name != "actual.wall_ms"
                }
                for group in report["statistics"]["groups"]
            ]
        )
        measurements.append(
            [
                {
                    "group": trial["group"],
                    "verdict": trial["verdict"],
                    "new_usage": trial["replay"]["new_usage"],
                    "model_calls": trial["replay"]["actual"]["model_calls"],
                    "tool_calls": trial["replay"]["actual"]["tool_calls"],
                    "unknown_outcomes": trial["replay"]["actual"]["unknown_outcomes"],
                }
                for trial in report["trials"]
            ]
        )
    assert measurements[0] == measurements[1]
    assert statistical_metrics[0] == statistical_metrics[1]
    assert all(trial["verdict"] == "passed" for trial in measurements[0])


def test_unrecoverable_custom_corpus_is_rejected_instead_of_using_bundled_sources(
    history, tmp_path
):
    bundled = Corpus.bundled()
    first, *others = tuple(bundled.sources.values())
    changed_body = first.body + "\n自定义固定语料补充。"
    changed = evolve(
        first, body=changed_body, body_sha256=hashlib.sha256(changed_body.encode()).hexdigest()
    )
    custom = Corpus((changed, *others))
    research = ResearchService(history, tmp_path / "custom", corpus=custom)
    repo = history.create_repository("自定义同步来源")
    goal = ResearchGoal(text="比较三个 fixture 框架")
    config = research.create_config(repo)
    task = research.create_task(repo, config, goal)
    session = research.start(repo, task)
    assert session.execute().evaluation.passed
    manager = ManagementService(research)
    checkpoint = manager.commits(repo)["items"][-1]["commit_id"]
    with pytest.raises(AgentGitError) as caught:
        manager.freeze_validator(
            repo,
            FreezeValidator(
                source_run_id=str(session.run_id),
                from_commit=checkpoint,
            ),
            "unrecoverable-corpus",
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    # 默认语料验收器也不能掩盖来源环境中的真实语料摘要。
    ref = ValidatorService(history).freeze(repo, goal, bundled)
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(
            repo,
            NewExperiment(
                source_run_id=str(session.run_id),
                from_commit=checkpoint,
                validator_ref=ref,
                repeats=1,
                budget=BudgetLimits(),
                groups=(
                    ExperimentGroup(key="A", label="A"),
                    ExperimentGroup(key="B", label="B"),
                ),
            ),
            "wrong-default-corpus",
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["experiments"])
            ).scalar_one()
            == 0
        )
