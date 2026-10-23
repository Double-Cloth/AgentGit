"""真实历史/CAS 的只读行为图；缺失与越权不能变成完整比较。"""

import json
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_management_api import TOKEN
from test_recorded_replayer import replay_source as replay_source
from test_versions import fork
from test_versions import versioned as versioned
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.diagnosis.graph import contextual_edges
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.history import EventInput
from agentgit.services.management import ManagementService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
TABLES = build_metadata().tables


def test_real_trace_self_comparison_verifies_plan_call_context_and_is_readonly(
    history, replay_source, monkeypatch
):
    session, _, _, _ = replay_source
    service = BehaviorService(history)
    before = history.branch(session.repository_id)
    with history.engine.connect() as connection:
        counts = {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in ("events", "commits", "objects", "object_refs", "jobs", "external_calls")
        }

    def forbidden(*args, **kwargs):
        pytest.fail("行为读取不能写 CAS、排队或执行模型/工具")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    monkeypatch.setattr(Worker, "run_once", forbidden)
    monkeypatch.setattr(history.objects, "put", forbidden)
    trace = service.trace(session.repository_id, session.run_id)
    assert len(trace.nodes) > 40
    assert {edge.basis for edge in trace.edges} >= {
        "caused_by",
        "call_occurrence",
        "decision_context",
        "plan_dependency",
    }
    assert not trace.gaps, trace.gaps
    report = service.compare(session.repository_id, session.run_id, session.run_id)
    assert report["comparison"]["complete_evidence"]
    assert all(item["comparison"] == "equal" for item in report["comparison"]["matches"])
    assert not report["comparison"]["first_left"]
    assert history.branch(session.repository_id) == before
    with history.engine.connect() as connection:
        assert counts == {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in counts
        }


def test_missing_owned_event_payload_is_a_gap_and_never_complete(history, replay_source):
    session, _, _, _ = replay_source
    row = next(
        item
        for item in history.events(session.repository_id, session.run_id, limit=1000)
        if item["type"] == "llm.requested"
    )
    path = history.objects.path_for(row["payload_ref"])
    payload = path.read_bytes()
    path.unlink()
    try:
        result = BehaviorService(history).compare(
            session.repository_id, session.run_id, session.run_id
        )
        assert not result["comparison"]["complete_evidence"]
        assert any(item["reason"] == "missing_payload" for item in result["left"]["gaps"])
        assert any(item["comparison"] == "unverified" for item in result["comparison"]["matches"])
    finally:
        path.write_bytes(payload)


def test_unowned_event_is_rejected_before_reading_its_bytes(history, replay_source, monkeypatch):
    session, _, _, _ = replay_source
    row = next(
        item
        for item in history.events(session.repository_id, session.run_id, limit=1000)
        if item["type"] == "llm.requested"
    )
    refs = TABLES["object_refs"]
    with history.engine.begin() as connection:
        # 仅在 fixture 独立测试 schema 内模拟索引损坏；生产不可变保护保持启用。
        connection.execute(sa.text("ALTER TABLE object_refs DISABLE TRIGGER USER"))
        connection.execute(
            refs.delete().where(
                refs.c.repository_id == session.repository_id,
                refs.c.owner_type == "event",
                refs.c.owner_id == f"{session.run_id}:{row['seq']}",
                refs.c.role == "payload",
            )
        )
        connection.execute(sa.text("ALTER TABLE object_refs ENABLE TRIGGER USER"))
    original = history.objects.get

    def checked(ref, kind):
        assert ref != row["payload_ref"], "不能读取无当前项目事件归属的内容"
        return original(ref, kind)

    monkeypatch.setattr(history.objects, "get", checked)
    with pytest.raises(AgentGitError) as error:
        BehaviorService(history).trace(session.repository_id, session.run_id)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_cross_project_run_is_rejected_and_event_bound_does_not_truncate(
    history, replay_source, monkeypatch
):
    session, _, _, _ = replay_source
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError) as error:
        BehaviorService(history).trace(other, session.run_id)
    assert error.value.code == ErrorCode.NOT_FOUND
    monkeypatch.setattr("agentgit.services.diagnosis.MAX_TRACE_EVENTS", 1)
    with pytest.raises(AgentGitError) as error:
        BehaviorService(history).trace(session.repository_id, session.run_id)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


def test_committed_forks_use_shared_ancestor_and_preserve_parallel_divergences(versioned):
    versions, repo, source, commits, environment = versioned
    before = versions.history.branch(repo)
    runs = []
    for name, value in (("A", 1), ("B", 2)):
        fork(versions, repo, source, commits[1], name)
        run = versions.history.create_run(repo, name)
        versions.history.commit(
            repo,
            run,
            expected_head=commits[1],
            expected_version=0,
            state={"fixture": value},
            environment=environment,
            events=(
                EventInput("fixture.input", "input", {"value": value}),
                EventInput("fixture.parallel", "parallel", {"value": value}),
                EventInput("fixture.report", "report", {"value": value}, caused_by_seq=1),
            ),
        )
        runs.append(run)
    report = BehaviorService(versions.history).compare(repo, *runs)
    result = report["comparison"]
    assert result["common_ancestor"] == commits[1]
    assert len(result["matches"]) == 3
    assert {item["seq"] for item in result["first_left"]} == {1, 2}
    assert {item["seq"] for item in result["first_right"]} == {1, 2}
    assert not result["complete_evidence"]  # fixture 仍处于运行中。
    assert versions.history.branch(repo) == before


def test_uncommitted_tail_and_cross_run_cause_remain_explicit(versioned):
    versions, repo, source, commits, _ = versioned
    fork(versions, repo, source, commits[0], "tail")
    run = versions.history.create_run(repo, "tail")
    versions.history.append_event(
        repo,
        run,
        EventInput("fixture.tail", "tail", {}, caused_by_run_id=source, caused_by_seq=1),
    )
    trace = BehaviorService(versions.history).trace(repo, run)
    assert not trace.scope_complete and len(trace.nodes) == 1
    assert {gap.reason for gap in trace.gaps} == {"uncommitted_event", "outside_trace"}
    related = next(gap.related for gap in trace.gaps if gap.reason == "outside_trace")
    assert related.run_id == str(source) and related.event_id is not None


def test_corrupt_owned_payload_and_output_limits_fail_without_prefix(versioned, monkeypatch):
    versions, repo, run, _, _ = versioned
    service = BehaviorService(versions.history)
    row = versions.history.events(repo, run)[0]
    path = versions.history.objects.path_for(row["payload_ref"])
    original = path.read_bytes()
    path.write_bytes(b"corrupted")
    try:
        with pytest.raises(AgentGitError) as error:
            service.trace(repo, run)
        assert error.value.code == ErrorCode.OBJECT_CORRUPT
    finally:
        path.write_bytes(original)
    monkeypatch.setattr("agentgit.services.diagnosis.MAX_REPORT_BYTES", 1)
    with pytest.raises(AgentGitError) as error:
        service.trace(repo, run)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


def test_http_cli_agree_and_reject_unauthorized_foreign_and_invalid_runs(
    history, replay_source, monkeypatch
):
    session, _, _, _ = replay_source
    manager = ManagementService(session.service)
    repo, run = str(session.repository_id), str(session.run_id)
    other = history.create_repository("不可读来源")
    before = history.branch(session.repository_id)

    def forbidden(*args, **kwargs):
        pytest.fail("只读 API/CLI 不得写 CAS 或执行 Job/模型/工具")

    monkeypatch.setattr(history.objects, "put", forbidden)
    monkeypatch.setattr(Worker, "run_once", forbidden)
    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)

    @contextmanager
    def current(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", current)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        path = f"/runs/{run}/behavioral-trace"
        query = {"left_run_id": run, "right_run_id": run}
        headers = {"X-Repository-ID": repo}
        trace = client.get(path, headers=headers)
        report = client.get("/behavioral-diff", params=query, headers=headers)
        assert trace.status_code == report.status_code == 200, (trace.text, report.text)
        assert report.json()["comparison"]["complete_evidence"]
        for command, arguments, expected in (
            ("behavioral-trace", [repo, run], trace.json()),
            ("behavioral-diff", [repo, run, run], report.json()),
        ):
            result = CliRunner().invoke(app, [command, *arguments])
            assert result.exit_code == 0, (result.stdout, result.exception)
            assert json.loads(result.stdout) == expected
        assert (
            client.get(path, headers={**headers, "Authorization": "Bearer invalid"}).status_code
            == 401
        )
        assert client.get(path, headers={"X-Repository-ID": str(other)}).status_code == 404
        assert (
            client.get(
                "/behavioral-diff", params=query, headers={"X-Repository-ID": str(other)}
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/behavioral-diff", params={**query, "right_run_id": "invalid"}, headers=headers
            ).status_code
            == 422
        )
        # 既有 /runs/{id}/{action} 会捕获未知动作并按固定 action 契约拒绝。
        assert client.post(path, headers=headers, json={}).status_code == 422
    invalid = CliRunner().invoke(app, ["behavioral-diff", repo, run, "invalid"])
    assert invalid.exit_code != 0 and json.loads(invalid.stdout)["error"]["code"] == "validation"
    assert history.branch(session.repository_id) == before


def test_missing_owned_response_keeps_recorded_event_but_marks_observation_unverified(
    history, replay_source
):
    session, _, _, _ = replay_source
    row = next(
        item
        for item in history.events(session.repository_id, session.run_id, limit=1000)
        if item["type"] == "llm.completed"
    )
    payload = history.objects.get_json(row["payload_ref"], ObjectType.EVENT)["data"]
    outcome = parse_contract(
        history.objects.get(payload["result_ref"], ObjectType.CALL), SavedCallOutcome
    )
    path = history.objects.path_for(outcome.response_ref)
    original = path.read_bytes()
    path.unlink()
    try:
        graph = BehaviorService(history).trace(session.repository_id, session.run_id)
        event = next(node for node in graph.nodes if node.event.seq == row["seq"])
        assert event.occurrence is not None and not event.payload_verified
        assert event.observation_ref is None
        assert any(
            gap.event == event.event and gap.reason == "missing_observation" for gap in graph.gaps
        )
    finally:
        path.write_bytes(original)


def test_model_context_without_matching_saved_request_cannot_supply_inferred_edges(
    history, replay_source, monkeypatch
):
    session, _, _, _ = replay_source

    def without_verified_model_context(nodes, states, state_events, checkpoints, verified):
        assert verified, "正例必须先通过真实请求与 checkpoint 状态的相等验证"
        return contextual_edges(nodes, states, state_events, checkpoints, set())

    monkeypatch.setattr(
        "agentgit.services.diagnosis.contextual_edges", without_verified_model_context
    )
    graph = BehaviorService(history).trace(session.repository_id, session.run_id)
    model_requests = {
        node.event.key
        for node in graph.nodes
        if node.phase == "call.request" and node.occurrence.provider.startswith("llm:")
    }
    assert model_requests
    assert not any(
        edge.target.key in model_requests and edge.basis == "decision_context"
        for edge in graph.edges
    )
    assert model_requests <= {
        gap.event.key for gap in graph.gaps if gap.reason == "unverified_context"
    }


def test_identical_logical_steps_without_shared_version_ancestor_do_not_align(versioned):
    versions, repo, source, _, environment = versioned
    with versions.history.engine.begin() as connection:
        connection.execute(TABLES["branches"].insert().values(repository_id=repo, name="root"))
    run = versions.history.create_run(repo, "root")
    versions.history.commit(
        repo,
        run,
        expected_head=None,
        expected_version=0,
        state={"number": 0},
        environment=environment,
        events=(EventInput("fixture.step", "step-0", {}),),
    )
    report = BehaviorService(versions.history).compare(repo, source, run)
    assert report["comparison"]["status"] == "no_shared_ancestor"
    assert not report["comparison"]["matches"]
    assert not report["comparison"]["complete_evidence"]
    assert report["left"]["anchor_commit"] is report["right"]["anchor_commit"] is None


@pytest.mark.parametrize("limit", ["MAX_TRACE_EDGES", "MAX_TRACE_BYTES"])
def test_dependency_and_content_bounds_reject_the_whole_trace(
    history, replay_source, monkeypatch, limit
):
    session, _, _, _ = replay_source
    monkeypatch.setattr(f"agentgit.services.diagnosis.{limit}", 1)
    with pytest.raises(AgentGitError) as error:
        BehaviorService(history).trace(session.repository_id, session.run_id)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT
