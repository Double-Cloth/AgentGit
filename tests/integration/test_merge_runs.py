"""已批准状态的独立分支、新任务、显式录制路径和耐久边界。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from merge_proposal_cases import archive, rows_for
from merge_proposal_cases import merge_case as merge_case
from test_merge_approval import prepare

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge_runs import ForkMergedBranch, NewMergedRun
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, BudgetUsage, ResearchGoal, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import RecordedReplay
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def approved(merge_case):
    repo, research, manager, pins, session = merge_case
    identity, _, _, _, request = prepare(manager.merge_proposals, repo, pins, actor=manager.actor)
    result = manager.approve_merge_proposal(repo, identity, request, "approve")
    return repo, research, manager, pins, session, result


def fork_and_request(case, name="merged-work"):
    repo, research, manager, pins, session, approval = case
    fork = ForkMergedBranch(
        approval_id=approval["decision_id"], commit_id=approval["commit_id"], name=name
    )
    branch = manager.fork_merged_branch(repo, fork, "fork-" + name)
    config = research.create_config(repo, session.config)
    with research.history.engine.connect() as connection:
        version = connection.execute(
            sa.select(TABLES["agent_configs"].c.version).where(
                TABLES["agent_configs"].c.id == config
            )
        ).scalar_one()
    request = NewMergedRun(
        branch_name=name,
        expected_head=branch["head_commit_id"],
        expected_branch_version=0,
        config_id=str(config),
        config_version=version,
        validator_ref=pins.validator_ref,
        budget=session.config.limits,
    )
    return fork, request


def source_unchanged(before, after):
    for name in (
        "external_calls",
        "run_commits",
        "snapshots",
        "events",
        "sandbox_executions",
        "commits",
        "commit_parents",
        "artifacts",
    ):
        assert all(item in after[name] for item in before[name]), name


def test_merge_fork_new_run_and_recorded_explicit_path(history, merge_case, tmp_path, request):
    case = approved(merge_case)
    repo, research, manager, pins, _, approval = case
    before = rows_for(history, repo)
    fork, payload = fork_and_request(case)
    assert manager.fork_merged_branch(repo, fork, "fork-merged-work")["origin_kind"] == "merge"
    branches = {item["name"]: item for item in manager.branches(repo)["items"]}
    assert branches[payload.branch_name]["merge_commit_id"] == approval["commit_id"]
    created = manager.start_merged_run(repo, payload, "start")
    job = manager.jobs.get(repo, UUID(created["job_id"]))
    spec = manager.jobs.payload(job).merged
    assert spec is not None and job["origin_run_id"] is None
    initial = parse_contract(
        history.objects.get(spec.initial_state_ref, ObjectType.STATE), StateSnapshot
    )
    state = parse_contract(encode(initial.data), AgentState)
    assert state.completed == state.messages == state.stopped_executions == ()
    assert state.usage == BudgetUsage() and state.pending_tool is None and state.evaluation is None
    merged = history.restore(repo, approval["commit_id"])
    assert initial.artifacts == merged.artifacts and state.working_memory
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    assert manager.start_merged_run(repo, payload, "start") == created
    with history.engine.connect() as connection:
        links = TABLES["run_commits"]
        checkpoints = list(
            connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == UUID(created["run_id"]))
                .order_by(links.c.run_order)
            ).scalars()
        )
    assert checkpoints
    events = history.events(repo, UUID(created["run_id"]))
    assert sum(item["type"] == "merge.initialized" for item in events) == 1
    with pytest.raises(AgentGitError) as error:
        manager.replay(
            repo,
            RecordedReplay(
                source_run_id=created["run_id"],
                from_commit=approval["commit_id"],
                until_commit=checkpoints[-1],
                branch_name="invalid-recorded",
            ),
            "invalid-recorded",
        )
    assert error.value.code == ErrorCode.RECORDING_MISSING
    before_recorded = rows_for(history, repo)
    replayed = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=created["run_id"],
            from_commit=checkpoints[0],
            until_commit=checkpoints[-1],
            branch_name="recorded-merged",
        ),
        "recorded",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    after = rows_for(history, repo)
    assert after["external_calls"] == before_recorded["external_calls"]
    assert history.branch(repo, pins.ours.name)["head_commit_id"] == approval["commit_id"]
    assert history.branch(repo, pins.theirs.name)["head_commit_id"] == pins.theirs.head
    source_unchanged(before, after)
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(approval=approval, created=created, replayed=replayed, checkpoints=checkpoints),
    )


def test_merge_run_rejects_expansion_stale_binding_and_plain_entry(
    history, merge_case, tmp_path, request
):
    case = approved(merge_case)
    repo, research, manager, _, session, _ = case
    _, payload = fork_and_request(case)
    wider = evolve(
        session.config,
        limits=evolve(session.config.limits, max_steps=session.config.limits.max_steps + 1),
    )
    config = research.create_config(repo, wider)
    with history.engine.connect() as connection:
        version = connection.execute(
            sa.select(TABLES["agent_configs"].c.version).where(
                TABLES["agent_configs"].c.id == config
            )
        ).scalar_one()
    before = rows_for(history, repo)
    for number, override in enumerate(
        (
            dict(expected_head="0" * 64),
            dict(expected_branch_version=1),
            dict(config_version=payload.config_version + 1),
            dict(
                config_id=str(config),
                config_version=version,
                budget=wider.limits.model_dump(mode="json"),
            ),
        )
    ):
        bad = parse_contract(encode({**payload.model_dump(mode="json"), **override}), NewMergedRun)
        with pytest.raises(AgentGitError):
            manager.start_merged_run(repo, bad, "bad-" + str(number))
    wrong_goal = ValidatorService(history).freeze(
        repo, ResearchGoal(text="不同目标"), research.corpus
    )
    with pytest.raises(AgentGitError):
        manager.start_merged_run(repo, evolve(payload, validator_ref=wrong_goal), "wrong-goal")
    after = rows_for(history, repo)
    for name in ("tasks", "runs", "jobs", "merge_run_requests", "external_calls"):
        assert after[name] == before[name]
    task = research.create_task(repo, UUID(payload.config_id), session.state.goal)
    with pytest.raises(AgentGitError):
        manager.jobs.enqueue(
            repo,
            task,
            actor=manager.actor,
            key="plain",
            config_version=payload.config_version,
            expected_branch_version=0,
            expected_head=payload.expected_head,
            branch_name=payload.branch_name,
        )
    with pytest.raises(AgentGitError):
        research.start(repo, task, branch_name=payload.branch_name)
    with pytest.raises(AgentGitError):
        manager.start_merged_run(uuid4(), payload, "wrong-project")
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(result="rejected", tested_bindings=7),
    )


def test_merge_run_same_key_and_competing_keys_publish_one_task_per_branch(
    history, merge_case, tmp_path, request
):
    case = approved(merge_case)
    repo, _, manager, _, _, _ = case
    _, payload = fork_and_request(case)
    with ThreadPoolExecutor(2) as pool:
        repeated = list(
            pool.map(lambda _: manager.start_merged_run(repo, payload, "same-key"), range(2))
        )
    assert repeated[0] == repeated[1]
    _, race = fork_and_request(case, "competing")

    def submit(key):
        try:
            return manager.start_merged_run(repo, race, key)
        except AgentGitError:
            return None

    with ThreadPoolExecutor(2) as pool:
        competing = list(pool.map(submit, ("race-one", "race-two")))
    assert sum(value is not None for value in competing) == 1
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    rows = rows_for(history, repo)
    assert len(rows["merge_run_requests"]) == 2
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(repeated=repeated, competing=competing),
    )


def test_merge_run_publication_rolls_back_and_original_key_retries(
    history, merge_case, tmp_path, request
):
    case = approved(merge_case)
    repo, _, manager, _, _, _ = case
    _, payload = fork_and_request(case)
    before = rows_for(history, repo)

    def fail(point):
        if point == "merge.run_stored":
            raise RuntimeError("独立新任务发布中断")

    history.fault = fail
    with pytest.raises(RuntimeError, match="发布中断"):
        manager.start_merged_run(repo, payload, "start")
    history.fault = lambda _: None
    assert rows_for(history, repo) == before
    created = manager.start_merged_run(repo, payload, "start")
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    assert ManagementService(manager.research).start_merged_run(repo, payload, "start") == created
    archive(history, repo, tmp_path, request.node.name + "/final", dict(created=created))
