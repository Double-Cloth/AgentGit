"""S3：真实数据库中的完整离线研究与故障闭环。"""

import socket
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass, RunStatus, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetLimits, ResearchGoal, evolve
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.research import TABLES, ResearchService

pytestmark = pytest.mark.integration


def session_for(history, tmp_path, *, config=None, corpus=None):
    service = ResearchService(history, tmp_path / "research", corpus=corpus)
    repository = history.create_repository("研究")
    config_id = service.create_config(repository, config)
    task = service.create_task(repository, config_id, ResearchGoal(text="比较三个 fixture 框架"))
    return service.start(repository, task)


def test_research_completes_with_durable_sources_and_artifacts(history, tmp_path):
    session = session_for(history, tmp_path)
    state = session.execute()
    assert state.status == RunStatus.COMPLETED
    assert state.evaluation.passed
    assert set(session.workspace.contents()) == {"report.md", "sources.json"}
    assert len(state.working_memory) == 6
    assert state.usage.model_calls == 11
    assert state.usage.tool_calls == 9
    with history.engine.connect() as connection:
        commits = (
            connection.execute(
                sa.select(TABLES["run_commits"].c.commit_id)
                .where(TABLES["run_commits"].c.run_id == session.run_id)
                .order_by(TABLES["run_commits"].c.run_order)
            )
            .scalars()
            .all()
        )
        run = (
            connection.execute(
                sa.select(TABLES["runs"]).where(TABLES["runs"].c.id == session.run_id)
            )
            .mappings()
            .one()
        )
        assert run["status"] == "completed"
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["external_calls"])
            ).scalar_one()
            == 20
        )
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["task_nodes"])
                .where(TABLES["task_nodes"].c.status == "completed")
            ).scalar_one()
            == 9
        )
    for commit in commits:
        snapshot = history.restore(session.repository_id, commit)
        restored = parse_contract(encode(snapshot.data), AgentState)
        assert restored.goal == state.goal
    assert len(commits) == 21
    report = session.workspace.contents()["report.md"]
    assert (
        history.read_artifact(
            session.repository_id, history.artifacts(session.repository_id, session.head)[0]["id"]
        )
        == report
    )


def test_two_runs_have_identical_logic_and_isolated_files_without_network(history, tmp_path):
    service = ResearchService(history, tmp_path / "research")
    repository = history.create_repository("双运行")
    config = service.create_config(repository)
    sessions, states, snapshots = [], [], []
    with (
        patch.object(
            socket.socket, "connect", side_effect=AssertionError("禁止真实网络")
        ) as connect,
        patch("socket.create_connection", side_effect=AssertionError("禁止真实网络")) as create,
    ):
        for _ in range(2):
            task = service.create_task(
                repository, config, ResearchGoal(text="比较三个 fixture 框架")
            )
            session = service.start(repository, task)
            states.append(session.execute())
            snapshots.append(history.restore(repository, session.head))
            sessions.append(session)
    assert connect.call_count == create.call_count == 0
    assert states[0] == states[1]
    assert snapshots[0] == snapshots[1]
    assert sessions[0].run_id != sessions[1].run_id
    assert sessions[0].workspace.root != sessions[1].workspace.root
    sessions[0].workspace.write("report.md", "只改变第一个工作区", "text/markdown")
    assert (
        sessions[1].workspace.contents()["report.md"]
        != sessions[0].workspace.contents()["report.md"]
    )
    for session in sessions:
        with history.engine.connect() as connection:
            commits = (
                connection.execute(
                    sa.select(TABLES["run_commits"].c.commit_id).where(
                        TABLES["run_commits"].c.run_id == session.run_id
                    )
                )
                .scalars()
                .all()
            )
        for commit in commits:
            parse_contract(encode(history.restore(repository, commit).data), AgentState)


def test_transient_tool_failure_replans_without_rewriting_old_plan(history, tmp_path):
    session = session_for(history, tmp_path)
    invoke = session.tools.invoke
    failed = False

    def transient(name, arguments):
        nonlocal failed
        if name == "source_read" and not failed:
            failed = True
            raise AgentGitError(ErrorCode.TOOL_ERROR, "合成只读故障")
        return invoke(name, arguments)

    session.tools.invoke = transient
    state = session.execute()
    assert state.status == RunStatus.COMPLETED
    assert state.usage.replans == 1
    assert state.plan_version == 2
    assert state.attempts["read-0"] == 2
    with history.engine.connect() as connection:
        nodes = (
            connection.execute(
                sa.select(TABLES["task_nodes"])
                .where(TABLES["task_nodes"].c.node_key == "read-0")
                .order_by(TABLES["task_nodes"].c.plan_version)
            )
            .mappings()
            .all()
        )
        assert [node["status"] for node in nodes] == ["pending", "completed"]


def test_config_and_task_cross_project_relationships_are_rejected(history, tmp_path):
    service = ResearchService(history, tmp_path / "research")
    repo, other = history.create_repository("A"), history.create_repository("B")
    config = service.create_config(repo)
    other_config = service.create_config(other)
    with pytest.raises(AgentGitError):
        service.create_task(other, config, ResearchGoal(text="比较三个 fixture 框架"))
    task = service.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    with pytest.raises(AgentGitError):
        service.start(other, task)
    session = service.start(repo, task)
    with pytest.raises(AgentGitError) as caught:
        service.start(repo, task)
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    with history.engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            TABLES["runs"]
            .update()
            .where(TABLES["runs"].c.id == session.run_id)
            .values(agent_config_id=other_config)
        )
    with history.engine.begin() as connection, pytest.raises(DBAPIError, match="immutable_history"):
        connection.execute(
            TABLES["agent_configs"]
            .update()
            .where(TABLES["agent_configs"].c.id == config)
            .values(version=2)
        )


def prepared_gateway(history, tmp_path):
    session = session_for(history, tmp_path)
    session.environment = session._environment(MockResearchModel())
    session.save(evolve(session.state, status=RunStatus.PLANNING), "run.planning", {})
    return session, RecordedCallGateway(
        history, session.repository_id, session.run_id, lambda: (session.head, session.version)
    )


def test_call_result_is_reused_and_missing_response_has_no_fallback(history, tmp_path):
    session, gateway = prepared_gateway(history, tmp_path)
    calls = []

    def invoke():
        calls.append(1)
        # 若调用期间还持有 Run 锁，第二个连接的 NOWAIT 会立即失败。
        with history.engine.begin() as connection:
            connection.execute(
                sa.select(TABLES["runs"].c.id)
                .where(TABLES["runs"].c.id == session.run_id)
                .with_for_update(nowait=True)
            )
        return b"recorded response"

    options = dict(
        step="recording",
        attempt=1,
        provider="tool:fixture/v1",
        signature="a" * 64,
        effect=EffectClass.READ_ONLY,
        request=b"request",
        invoke=invoke,
        max_bytes=128,
    )
    first = gateway.call(**options)
    assert gateway.call(**options) == first
    assert calls == [1]
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**{**options, "request": b"changed"})
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**{**options, "max_bytes": 256})
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    history.objects.path_for(first.trace.response_ref).unlink()
    with pytest.raises(AgentGitError):
        gateway.call(**options)
    assert calls == [1]


def test_started_call_without_result_is_never_blindly_reissued(history, tmp_path):
    session, gateway = prepared_gateway(history, tmp_path)
    calls = []

    def crash():
        calls.append(1)
        raise SystemExit(73)

    options = dict(
        step="interrupted",
        attempt=1,
        provider="tool:fixture/v1",
        signature="a" * 64,
        effect=EffectClass.READ_ONLY,
        request=b"request",
        invoke=crash,
        max_bytes=128,
    )
    with pytest.raises(SystemExit):
        gateway.call(**options)
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert calls == [1]


def test_task_projection_rolls_back_with_failed_history_commit(history, tmp_path):
    session = session_for(history, tmp_path)

    def fail(point):
        if point == "in_transaction":
            raise RuntimeError("注入投影故障")

    history.fault = fail
    with pytest.raises(RuntimeError):
        session.execute()
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(TABLES["runs"].c.status).where(TABLES["runs"].c.id == session.run_id)
            ).scalar_one()
            == "queued"
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["commits"])
            ).scalar_one()
            == 0
        )


def test_truncated_recording_keeps_real_length_and_never_claims_complete(history, tmp_path):
    session, gateway = prepared_gateway(history, tmp_path)
    outcome = gateway.call(
        step="overflow",
        attempt=1,
        provider="llm:fixture/v1",
        signature="a" * 64,
        effect=EffectClass.READ_ONLY,
        request=b"request",
        invoke=lambda: b"x" * 129,
        max_bytes=128,
    )
    assert len(outcome.payload) == 128
    assert outcome.output_bytes == 129
    assert outcome.error.code == ErrorCode.OUTPUT_LIMIT


def test_terminal_retry_uses_new_run_and_new_plan_version(history, tmp_path):
    # 首次运行在已完成部分计划后耗尽预算；第二次同条件也受同一上限约束。
    first = session_for(history, tmp_path, config=AgentConfig(limits=BudgetLimits(max_steps=3)))
    assert first.execute().status == RunStatus.BUDGET_EXHAUSTED
    with history.engine.connect() as connection:
        old = [
            dict(row)
            for row in connection.execute(
                sa.select(TABLES["task_nodes"])
                .where(
                    TABLES["task_nodes"].c.task_id == first.task_id,
                    TABLES["task_nodes"].c.plan_version == 1,
                )
                .order_by(TABLES["task_nodes"].c.node_key)
            ).mappings()
        ]
    second = first.service.start(first.repository_id, first.task_id)
    assert second.run_id != first.run_id
    assert second.state.plan_version == 1
    assert second.execute().status == RunStatus.BUDGET_EXHAUSTED
    with history.engine.connect() as connection:
        after = [
            dict(row)
            for row in connection.execute(
                sa.select(TABLES["task_nodes"])
                .where(
                    TABLES["task_nodes"].c.task_id == first.task_id,
                    TABLES["task_nodes"].c.plan_version == 1,
                )
                .order_by(TABLES["task_nodes"].c.node_key)
            ).mappings()
        ]
        run = (
            connection.execute(
                sa.select(TABLES["runs"]).where(TABLES["runs"].c.id == second.run_id)
            )
            .mappings()
            .one()
        )
    assert old == after
    assert run["origin_run_id"] == first.run_id
    assert second.state.plan_version == 2
