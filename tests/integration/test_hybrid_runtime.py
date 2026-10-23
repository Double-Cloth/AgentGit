"""S7：真实数据库中 Hybrid 观测、错误、计量及零调用 Recorded。"""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_intervention_service import patch_request
from test_recorded_replayer import replay_source as replay_source

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.interventions import NewIntervention
from agentgit.core.management import ControlRun
from agentgit.core.models import parse_contract
from agentgit.core.research import BudgetLimits, ModelRequest, ToolDecision, evolve
from agentgit.core.versions import RecordedReplay
from agentgit.experiments.hybrid import HybridSelector
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


class SwappedReads(MockResearchModel):
    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        if "[swap-read-pair]" in request.prompt and request.phase == "act" and request.state.plan:
            ready = request.state.plan.ready(request.state.completed)
            if ready and ready[0].key in ("read-0", "read-1"):
                index = 1 if ready[0].key == "read-0" else 0
                return encode(
                    ToolDecision(
                        node_key=ready[0].key,
                        tool="source_read",
                        arguments={
                            "schema_version": 1,
                            "source_id": request.state.search_results[index],
                        },
                    ).model_dump(mode="json")
                )
        return super().invoke(payload)


def prepared(replay_source, *, miss="stop", swapped=False, base_index=3, groups=None):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    validator = ValidatorService(session.history).freeze(
        session.repository_id, session.state.goal, session.service.corpus
    )
    with session.history.engine.connect() as connection:
        selected = connection.execute(
            sa.select(TABLES["external_calls"].c.id).where(
                TABLES["external_calls"].c.run_id == session.run_id,
                TABLES["external_calls"].c.logical_step_id == "read-0",
            )
        ).scalar_one()
    request = NewExperiment(
        source_run_id=str(session.run_id),
        from_commit=commits[base_index],
        validator_ref=validator,
        repeats=1,
        budget=BudgetLimits(),
        hybrid=HybridPolicy(
            until_commit=commits[-1],
            calls=(str(selected),),
            miss_policy=miss,
        ),
        groups=groups
        or (
            ExperimentGroup(key="A", label="A"),
            ExperimentGroup(
                key="B",
                label="B",
                prompt=session.config.prompt + "[swap-read-pair]" if swapped else None,
            ),
        ),
    )
    return manager, request


def run_two(manager, *, model=None, fault=None):
    worker = Worker(manager.jobs, model_factory=model, fault=fault)
    return [worker.run_once(), worker.run_once()]


def assert_recorded(manager, repository, trial, monkeypatch):
    run_id = UUID(trial["run_id"])
    with manager.history.engine.connect() as connection:
        first = connection.execute(
            sa.select(TABLES["run_commits"].c.commit_id)
            .where(
                TABLES["run_commits"].c.run_id == run_id,
            )
            .order_by(TABLES["run_commits"].c.run_order)
            .limit(1)
        ).scalar_one()
    nonce = uuid4().hex
    replay = manager.replay(
        repository,
        RecordedReplay(
            source_run_id=str(run_id),
            from_commit=first,
            until_commit=trial["checkpoint_commit_id"],
            branch_name="recorded-hybrid-" + nonce,
        ),
        "recorded-hybrid-" + nonce,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded 不得调用模型、工具或 Hybrid 模拟器")

    with monkeypatch.context() as patch:
        patch.setattr(ToolRegistry, "invoke", forbidden)
        patch.setattr(HybridSelector, "resolve", forbidden)
        assert Worker(manager.jobs, model_factory=forbidden).run_once()["status"] == "completed"
    proof = manager.replay_status(repository, UUID(replay["job_id"]))["replay"]
    assert proof["verified"]
    assert proof["actual"]["model_calls"] == proof["actual"]["tool_calls"] == 0
    assert proof["logical_status"] == trial["status"]
    assert proof["test_only"] and proof["source_hybrid_policy_ref"]
    return replay


def test_selected_readonly_call_is_atomic_nonphysical_and_recorded_replays(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    original = history.branch(session.repository_id)
    result = manager.create_experiment(session.repository_id, request, "hybrid-hit")
    assert all(item["status"] == "completed" for item in run_two(manager))
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    assert report["manifest"]["mode"] == "hybrid"
    for trial in report["trials"]:
        assert trial["verdict"] == "passed"
        run = manager.jobs.run(session.repository_id, UUID(trial["run_id"]))
        assert run["mode"] == "hybrid"
        replay = trial["replay"]
        assert replay["actual"]["model_calls"] == 9
        assert replay["actual"]["tool_calls"] == 7
        assert replay["actual"]["recorded_observations"] == 1
        assert replay["new_usage"]["tool_calls"] == 8
        assert replay["observations"][0]["kind"] == "recorded"
        assert not replay["hybrid"]["unreached_call_ids"]
        assert_recorded(manager, session.repository_id, trial, monkeypatch)

    table = TABLES["hybrid_observations"]
    with history.engine.connect() as connection:
        observation = dict(connection.execute(sa.select(table)).mappings().first())
    for statement in (
        table.update().values(kind="fixture"),
        table.delete(),
        table.insert().values(**{**observation, "repository_id": uuid4(), "call_id": uuid4()}),
    ):
        with pytest.raises(DBAPIError), history.engine.begin() as connection:
            connection.execute(statement)
    assert history.branch(session.repository_id) == original


@pytest.mark.parametrize("selection", ["model", "write", "before", "foreign"])
def test_invalid_selection_is_rejected_without_partial_trials(history, replay_source, selection):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    calls = TABLES["external_calls"]
    if selection == "foreign":
        research = session.service
        repository = history.create_repository("其他项目")
        config = research.create_config(repository)
        task = research.create_task(repository, config, session.state.goal)
        source = research.start(repository, task)
        source.execute()
        query = sa.select(calls.c.id).where(
            calls.c.run_id == source.run_id, calls.c.logical_step_id == "read-0"
        )
    else:
        condition = {
            "model": calls.c.provider.like("llm:%"),
            "write": calls.c.effect == "IDEMPOTENT_WRITE",
            "before": calls.c.logical_step_id == "search",
        }[selection]
        query = sa.select(calls.c.id).where(calls.c.run_id == session.run_id, condition).limit(1)
    with history.engine.connect() as connection:
        identity = connection.execute(query).scalar_one()
        before = connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["runs"])
        ).scalar_one()
    assert request.hybrid is not None
    request = evolve(request, hybrid=evolve(request.hybrid, calls=(str(identity),)))
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(session.repository_id, request, "invalid-selection-" + selection)
    assert caught.value.code in (ErrorCode.RECORDING_MISSING, ErrorCode.FORBIDDEN)
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["runs"])).scalar_one()
            == before
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["experiments"])
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("miss", ["stop", "fixture"])
def test_changed_arguments_stop_or_simulate_without_old_answer_and_replay(
    history, replay_source, monkeypatch, miss
):
    session, *_ = replay_source
    manager, request = prepared(replay_source, miss=miss, swapped=True)
    result = manager.create_experiment(session.repository_id, request, "miss-" + miss)
    run_two(manager, model=SwappedReads)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    a, b = report["trials"]
    assert a["verdict"] == "passed"
    assert b["status"] == ("failed" if miss == "stop" else "completed")
    assert b["verdict"] == ("failed" if miss == "stop" else "passed")
    proof = b["replay"]["observations"][0]
    assert proof["kind"] == ("miss" if miss == "stop" else "fixture")
    assert not proof["matched"] and proof["test_only"]
    assert b["replay"]["actual"]["recorded_observations"] == 0
    assert b["replay"]["actual"]["tool_calls"] == (0 if miss == "stop" else 7)
    assert_recorded(manager, session.repository_id, b, monkeypatch)


def test_unreached_dependency_cannot_pass_and_its_failure_is_replayed(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    nodes = tuple(
        evolve(node, key="read-zero")
        if node.key == "read-0"
        else evolve(
            node,
            dependencies=tuple(
                "read-zero" if key == "read-0" else key for key in node.dependencies
            ),
        )
        for node in session.state.plan.nodes
    )
    manager, request = prepared(
        replay_source,
        groups=(
            ExperimentGroup(key="A", label="原计划"),
            ExperimentGroup(
                key="B", label="未到达选定节点", plan=evolve(session.state.plan, nodes=nodes)
            ),
        ),
    )
    result = manager.create_experiment(session.repository_id, request, "unreached")
    run_two(manager)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    b = report["trials"][1]
    assert b["status"] == "failed" and b["verdict"] != "passed"
    assert b["replay"]["hybrid"]["unreached_call_ids"] == list(request.hybrid.calls)
    assert_recorded(manager, session.repository_id, b, monkeypatch)


def test_synthetic_observation_is_visible_test_only_and_replayed_without_effect(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, body = patch_request(replay_source, kind="TOOL_RESPONSE_PATCH")
    patch = manager.create_intervention(
        session.repository_id, NewIntervention.model_validate_json(encode(body)), "synthetic-read"
    )
    manager, request = prepared(
        replay_source,
        base_index=4,
        groups=(
            ExperimentGroup(key="A", label="历史响应"),
            ExperimentGroup(
                key="B",
                label="合成观测",
                interventions=(patch["intervention_id"],),
            ),
        ),
    )
    original = history.branch(session.repository_id)
    result = manager.create_experiment(session.repository_id, request, "synthetic-experiment")
    assert all(item["status"] == "completed" for item in run_two(manager))
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    b = report["trials"][1]
    assert b["verdict"] == "passed" and report["test_only"]
    observation = b["replay"]["observations"][0]
    assert observation["kind"] == "synthetic" and observation["matched"]
    assert observation["test_only"] and observation["intervention_id"] == patch["intervention_id"]
    assert b["replay"]["actual"]["synthetic_observations"] == 1
    assert b["replay"]["actual"]["tool_calls"] == 7
    assert_recorded(manager, session.repository_id, b, monkeypatch)
    assert history.branch(session.repository_id) == original


def test_crash_after_atomic_observation_recovers_without_resolving_it_again(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    result = manager.create_experiment(session.repository_id, request, "atomic-observation")
    crashed = False

    def crash(point):
        nonlocal crashed
        with history.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["hybrid_observations"])
            ).scalar_one()
        if point == "after_result" and count and not crashed:
            crashed = True
            raise RuntimeError("Hybrid 观测已原子落盘")

    with pytest.raises(RuntimeError, match="原子落盘"):
        Worker(manager.jobs, fault=crash).run_once()
    with history.engine.begin() as connection:
        job_id = connection.execute(
            sa.select(TABLES["jobs"].c.id).where(TABLES["jobs"].c.status == "running")
        ).scalar_one()
        before = manager.jobs.get(session.repository_id, job_id)
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id != job_id)
            .values(available_at=sa.func.clock_timestamp() + sa.text("interval '1 hour'"))
        )
    original = HybridSelector.resolve

    def no_second_resolution(self, occurrence, *args):
        if occurrence.logical_step_id == "read-0":
            raise AssertionError("已保存的 Hybrid 观测被重新解析")
        return original(self, occurrence, *args)

    with monkeypatch.context() as patch:
        patch.setattr(HybridSelector, "resolve", no_second_resolution)
        assert Worker(manager.jobs).run_once()["status"] == "completed"
    after = manager.jobs.get(session.repository_id, job_id)
    assert after["run_id"] != before["run_id"]
    assert after["deadline_at"] == before["deadline_at"]
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    trial = next(item for item in report["trials"] if item["job_id"] == str(job_id))
    assert trial["verdict"] == "passed"
    assert trial["replay"]["actual"]["model_calls"] == 9
    assert trial["replay"]["actual"]["tool_calls"] == 7
    assert trial["replay"]["actual"]["recorded_observations"] == 1
    assert len(trial["replay"]["observations"]) == 1
    assert_recorded(manager, session.repository_id, trial, monkeypatch)


def test_missing_frozen_input_blocks_recorded_and_never_resends(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    result = manager.create_experiment(session.repository_id, request, "missing-hybrid-input")
    run_two(manager)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    trial = report["trials"][0]
    source_call = request.hybrid.calls[0]
    from agentgit.core.canonical import ObjectType
    from agentgit.core.models import CallIntent

    with history.engine.connect() as connection:
        intent_ref = connection.execute(
            sa.select(TABLES["external_calls"].c.intent_ref).where(
                TABLES["external_calls"].c.id == UUID(source_call)
            )
        ).scalar_one()
        first = connection.execute(
            sa.select(TABLES["run_commits"].c.commit_id)
            .where(TABLES["run_commits"].c.run_id == UUID(trial["run_id"]))
            .order_by(TABLES["run_commits"].c.run_order)
            .limit(1)
        ).scalar_one()
    intent = parse_contract(history.objects.get(intent_ref, ObjectType.CALL), CallIntent)
    history.objects.path_for(intent.request_ref).unlink()
    with pytest.raises(AgentGitError) as caught:
        manager.replay(
            session.repository_id,
            RecordedReplay(
                source_run_id=trial["run_id"],
                from_commit=first,
                until_commit=trial["checkpoint_commit_id"],
                branch_name="missing-policy-data",
            ),
            "missing-policy-data",
        )
    assert caught.value.code == ErrorCode.RECORDING_MISSING


@pytest.mark.parametrize("action", ["cancel", "budget"])
def test_control_and_budget_do_not_turn_unfinished_hybrid_into_passed(
    history, replay_source, monkeypatch, action
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    if action == "budget":
        request = evolve(request, budget=BudgetLimits(max_model_calls=1))
    result = manager.create_experiment(session.repository_id, request, "hybrid-control-" + action)
    if action == "cancel":
        for trial in result["trials"]:
            run_id = UUID(trial["run_id"])
            state = manager.jobs.run(session.repository_id, run_id)
            manager.control(
                session.repository_id,
                run_id,
                "cancel",
                ControlRun(expected_version=state["state_version"]),
                "cancel-" + str(run_id),
            )
    run_two(manager)
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    assert all(
        trial["status"] == ("cancelled" if action == "cancel" else "budget_exhausted")
        for trial in report["trials"]
    )
    assert all(trial["verdict"] == "incomplete" for trial in report["trials"])
    assert all(group["passed"] == 0 for group in report["groups"])
    assert_recorded(manager, session.repository_id, report["trials"][0], monkeypatch)


def test_unknown_model_result_stays_unknown_and_is_not_automatically_resent(
    history, replay_source, monkeypatch
):
    session, *_ = replay_source
    manager, request = prepared(replay_source)
    result = manager.create_experiment(session.repository_id, request, "unknown-hybrid-model")

    class UnknownModel(MockResearchModel):
        def invoke(self, payload):
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "确定性测试中的未知模型结果")

    run_two(manager, model=UnknownModel)
    assert Worker(manager.jobs, model_factory=UnknownModel).run_once() is None
    report = manager.experiment(session.repository_id, UUID(result["experiment_id"]))
    for trial in report["trials"]:
        assert trial["status"] == "outcome_unknown" and trial["verdict"] == "incomplete"
        assert trial["replay"]["actual"]["model_calls"] == 1
        assert trial["replay"]["actual"]["unknown_outcomes"] == 1
        assert trial["replay"]["actual"]["tool_calls"] == 0
    assert_recorded(manager, session.repository_id, report["trials"][0], monkeypatch)


def test_synthetic_fixture_repair_improves_outcome_but_does_not_rewrite_source(
    history, replay_source, monkeypatch
):
    clean, *_ = replay_source
    research = ResearchService(history, clean.service.data_root)
    repo = clean.repository_id
    with history.engine.connect() as connection:
        config_id = connection.execute(
            sa.select(TABLES["runs"].c.agent_config_id).where(TABLES["runs"].c.id == clean.run_id)
        ).scalar_one()
        correct_call = connection.execute(
            sa.select(TABLES["external_calls"].c.id).where(
                TABLES["external_calls"].c.run_id == clean.run_id,
                TABLES["external_calls"].c.logical_step_id == "read-0",
            )
        ).scalar_one()
    task = research.create_task(repo, config_id, clean.state.goal)
    source = research.start(repo, task)
    invoke = source.tools.invoke
    target_id = sorted(research.corpus.sources)[0]

    def stale(name, arguments):
        if name == "source_read" and arguments["source_id"] == target_id:
            original = research.corpus.read(target_id)
            value = evolve(original, valid_until="2020-01-01T00:00:00Z")
            return encode({"schema_version": 1, "source": value.model_dump(mode="json")})
        return invoke(name, arguments)

    source.tools.invoke = stale
    assert source.execute().status.value == "failed"
    manager = ManagementService(research)
    with history.engine.connect() as connection:
        commits = list(
            connection.execute(
                sa.select(TABLES["run_commits"].c.commit_id)
                .where(TABLES["run_commits"].c.run_id == source.run_id)
                .order_by(TABLES["run_commits"].c.run_order)
            ).scalars()
        )
        call_id = connection.execute(
            sa.select(TABLES["external_calls"].c.id).where(
                TABLES["external_calls"].c.run_id == source.run_id,
                TABLES["external_calls"].c.logical_step_id == "read-0",
            )
        ).scalar_one()
        event_id = connection.execute(
            sa.select(TABLES["events"].c.id).where(
                TABLES["events"].c.run_id == source.run_id,
                TABLES["events"].c.logical_step_id == "read-0",
                TABLES["events"].c.type == "tool.started",
            )
        ).scalar_one()
    ref = ValidatorService(history).freeze(repo, source.state.goal, research.corpus)
    context = manager.intervention_context(repo, source.run_id, commits[3], event_id, ref)
    patch = manager.create_intervention(
        repo,
        NewIntervention.model_validate_json(
            encode(
                {
                    "schema_version": 1,
                    "source_run_id": str(source.run_id),
                    "base_commit": commits[3],
                    "target_commit_id": context["target_commit_id"],
                    "target_event_id": event_id,
                    "operator": "TOOL_RESPONSE_PATCH",
                    "payload": {
                        "kind": "TOOL_RESPONSE_PATCH",
                        "replacement_call_id": str(correct_call),
                    },
                    "precondition": context["precondition"],
                    "effect_policy": "test_only",
                    "provenance": {"rationale": "用已记录的正确 fixture 观测替换失效观测"},
                }
            )
        ),
        "repair-fixture-observation",
    )
    before = history.branch(repo)
    result = manager.create_experiment(
        repo,
        NewExperiment(
            source_run_id=str(source.run_id),
            from_commit=commits[3],
            validator_ref=ref,
            repeats=1,
            budget=BudgetLimits(),
            hybrid=HybridPolicy(until_commit=commits[-1], calls=(str(call_id),)),
            groups=(
                ExperimentGroup(key="A", label="失败观测"),
                ExperimentGroup(
                    key="B", label="合成修正", interventions=(patch["intervention_id"],)
                ),
            ),
        ),
        "fixture-repair-experiment",
    )
    run_two(manager)
    report = manager.experiment(repo, UUID(result["experiment_id"]))
    assert [trial["verdict"] for trial in report["trials"]] == ["failed", "passed"]
    assert report["trials"][1]["replay"]["observations"][0]["kind"] == "synthetic"
    assert report["test_only"] and history.branch(repo) == before
    assert history.restore(repo, commits[-1]).data["status"] == "failed"
    recorded = assert_recorded(manager, repo, report["trials"][1], monkeypatch)
    job = manager.jobs.get(repo, UUID(recorded["job_id"]))
    chained = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=str(job["run_id"]),
            from_commit=recorded["from_commit"],
            until_commit=job["checkpoint_commit_id"],
            branch_name="compound-hybrid-synthetic",
        ),
        "compound-hybrid-synthetic",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    assert manager.replay_status(repo, UUID(chained["job_id"]))["replay"][
        "source_has_synthetic_patch"
    ]
