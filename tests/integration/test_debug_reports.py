"""真实失败硬断言、不可变报告、幂等入口与只读历史查询。"""

import json
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from diagnosis_dataset import load_dataset, run_case
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from test_management_api import TOKEN
from test_versions import versioned as versioned
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus
from agentgit.core.research import AgentState, Evaluation, ResearchGoal
from agentgit.services.history import TABLES, EventInput
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def failed_case(history, tmp_path):
    dataset = load_dataset()
    # 只选择开发集 D04 检查工程闭环；不使用保留集输出调参或计算命中率。
    case = next(case for case in dataset.cases if case.id == "D04" and case.split == "train")
    proof = run_case(history, tmp_path / "case", case, dataset)
    manager = ManagementService(ResearchService(history, tmp_path / "management"))
    return manager, proof, NewDebugReport(failed_run_id=proof["failed_run_id"])


def forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("定位报告不得执行模型、工具或 Worker")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    monkeypatch.setattr(Worker, "run_once", forbidden)


def test_no_reference_real_hard_assertions_and_report_creation_leave_source_history_unchanged(
    history, failed_case, monkeypatch
):
    manager, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    branch = history.branch(repo)
    names = ("events", "commits", "snapshots", "jobs", "runs", "external_calls", "run_commits")
    with history.engine.connect() as connection:
        counts = {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in names
        }
    forbid_execution(monkeypatch)
    with monkeypatch.context() as readonly:
        readonly.setattr(
            history.objects, "put", lambda *args, **kwargs: pytest.fail("编译不写 CAS")
        )
        report = manager.debug_reports.compile(repo, request)
    assert report.reference is report.comparison is None
    assert any(item.code == "source_expired:aster-overview" for item in report.diagnosis.assertions)
    assert report.diagnosis.mode == "static_only" and report.diagnosis.complete_evidence
    assert "write-report" in {item.action_step for item in report.diagnosis.candidates[:3]}
    created = manager.create_debug_report(repo, request, "create-no-reference")
    stored = manager.debug_report(repo, UUID(created["debug_report_id"]))
    assert stored["report"] == report.model_dump(mode="json")
    assert history.branch(repo) == branch
    with history.engine.connect() as connection:
        assert counts == {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in names
        }


def test_reference_input_anomaly_is_separate_and_stored_report_never_regrades(
    history, failed_case, monkeypatch
):
    manager, proof, _ = failed_case
    repo = UUID(proof["repository_id"])
    request = NewDebugReport(
        failed_run_id=proof["failed_run_id"], reference_run_id=proof["reference_run_id"]
    )
    forbid_execution(monkeypatch)
    result = manager.create_debug_report(repo, request, "with-reference")
    identity = UUID(result["debug_report_id"])
    before = manager.debug_report(repo, identity)
    report = before["report"]
    assert report["comparison"]["status"] == "comparable"
    assert report["environment_changes"] == ["input_ref"]
    anomalies = report["diagnosis"]["observed_anomalies"]
    assert any(
        item["event_id"] == proof["resolved_labels"]["first_visible"]["event_id"]
        for item in anomalies
    )
    candidate_ids = {item["event"]["event_id"] for item in report["diagnosis"]["candidates"]}
    assert all(item["event_id"] not in candidate_ids for item in anomalies)

    def forbidden(*args, **kwargs):
        pytest.fail("读取冻结报告不能按当前代码重新编译、定位、读取轨迹或写 CAS")

    monkeypatch.setattr(manager.debug_reports, "compile", forbidden)
    monkeypatch.setattr(manager.debug_reports.behavior, "trace", forbidden)
    monkeypatch.setattr(history.objects, "put", forbidden)
    monkeypatch.setattr("agentgit.services.debug_reports.static_diagnosis", forbidden)
    assert manager.debug_report(repo, identity) == before
    assert manager.create_debug_report(repo, request, "with-reference") == result


def test_http_cli_share_idempotency_and_report_project_boundary(history, failed_case, monkeypatch):
    manager, proof, request = failed_case
    repo = proof["repository_id"]
    other = history.create_repository("其他报告项目")
    forbid_execution(monkeypatch)

    @contextmanager
    def current(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", current)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        headers = {"X-Repository-ID": repo, "Idempotency-Key": "shared-report"}
        created = client.post(
            "/debug-reports", json=request.model_dump(mode="json"), headers=headers
        )
        assert created.status_code == 201, created.text
        result = CliRunner().invoke(
            app, ["debug-report", repo, proof["failed_run_id"], "--key", "shared-report"]
        )
        assert result.exit_code == 0, (result.stdout, result.exception)
        assert json.loads(result.stdout) == created.json()
        identity = created.json()["debug_report_id"]
        fetched = client.get(f"/debug-reports/{identity}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        result = CliRunner().invoke(app, ["debug-report-status", repo, identity])
        assert result.exit_code == 0 and json.loads(result.stdout) == fetched.json()
        assert (
            client.get(
                f"/debug-reports/{identity}", headers={"X-Repository-ID": str(other)}
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/debug-reports/{identity}",
                headers={"X-Repository-ID": repo, "Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/debug-reports",
                json={**request.model_dump(mode="json"), "gold_event": 42},
                headers={**headers, "Idempotency-Key": "forged"},
            ).status_code
            == 422
        )
        conflict = client.post(
            "/debug-reports",
            json={**request.model_dump(mode="json"), "reference_run_id": proof["reference_run_id"]},
            headers=headers,
        )
        assert conflict.status_code == 409
        referenced = CliRunner().invoke(
            app,
            [
                "debug-report",
                repo,
                proof["failed_run_id"],
                "--reference-run-id",
                proof["reference_run_id"],
                "--key",
                "cli-reference",
            ],
        )
        assert referenced.exit_code == 0, (referenced.stdout, referenced.exception)
        stored = manager.debug_report(
            UUID(repo), UUID(json.loads(referenced.stdout)["debug_report_id"])
        )
        assert stored["report"]["reference"]["run_id"] == proof["reference_run_id"]


def test_report_rows_are_immutable_and_cannot_bind_foreign_runs(history, failed_case):
    manager, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    created = manager.create_debug_report(repo, request, "immutable")
    reports = TABLES["debug_reports"]
    for statement in (reports.update().values(actor="changed"), reports.delete()):
        with (
            history.engine.begin() as connection,
            pytest.raises(DBAPIError, match="immutable_history"),
        ):
            connection.execute(statement.where(reports.c.id == UUID(created["debug_report_id"])))
    other = history.create_repository("外部 Run")
    foreign = history.create_run(other)
    identity = uuid4()
    with history.engine.begin() as connection, pytest.raises(DBAPIError):
        blob = history.objects.put(
            ObjectType.BLOB, history.objects.get(created["report_ref"], ObjectType.BLOB)
        )
        history._ref(connection, repo, blob, "debug_report", str(identity), "report")
        connection.execute(
            reports.insert().values(
                id=identity,
                repository_id=repo,
                failed_run_id=foreign,
                report_ref=created["report_ref"],
                actor="local-operator",
            )
        )


def test_missing_trace_payload_is_uncertain_and_does_not_become_a_root_cause(history, failed_case):
    manager, proof, request = failed_case
    repo, run = UUID(proof["repository_id"]), UUID(proof["failed_run_id"])
    node = next(
        item for item in history.events(repo, run, limit=1000) if item["type"] == "llm.requested"
    )
    path = history.objects.path_for(node["payload_ref"])
    original = path.read_bytes()
    path.unlink()
    try:
        report = manager.debug_reports.compile(repo, request)
        assert report.failed.gaps and not report.diagnosis.complete_evidence
        assert report.diagnosis.intervention_improvement is None
    finally:
        path.write_bytes(original)


def test_report_corruption_and_missing_owner_are_rejected_before_returning_content(
    history, failed_case, monkeypatch
):
    manager, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    result = manager.create_debug_report(repo, request, "read-boundary")
    identity = UUID(result["debug_report_id"])
    path = history.objects.path_for(result["report_ref"])
    original = path.read_bytes()
    path.write_bytes(b"corrupted")
    try:
        with pytest.raises(AgentGitError) as error:
            manager.debug_report(repo, identity)
        assert error.value.code == ErrorCode.OBJECT_CORRUPT
    finally:
        path.write_bytes(original)
    refs = TABLES["object_refs"]
    with history.engine.begin() as connection:
        # 仅在 fixture 独立测试 schema 模拟索引缺失，立即恢复不可变保护。
        connection.execute(sa.text("ALTER TABLE object_refs DISABLE TRIGGER USER"))
        connection.execute(
            refs.delete().where(
                refs.c.repository_id == repo,
                refs.c.owner_type == "debug_report",
                refs.c.owner_id == str(identity),
            )
        )
        connection.execute(sa.text("ALTER TABLE object_refs ENABLE TRIGGER USER"))
    monkeypatch.setattr(
        history.objects, "get", lambda *args, **kwargs: pytest.fail("无归属不能读取对象")
    )
    with pytest.raises(AgentGitError) as error:
        manager.debug_report(repo, identity)
    assert error.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("case_id,code", [("D06", "tool_error"), ("D10", "model_empty")])
def test_runtime_stops_produce_real_hard_assertions_without_fabricating_report_errors(
    history, tmp_path, monkeypatch, case_id, code
):
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.id == case_id and case.split == "train")
    proof = run_case(history, tmp_path / "runtime", case, dataset)
    manager = ManagementService(ResearchService(history, tmp_path / "management"))
    forbid_execution(monkeypatch)
    report = manager.debug_reports.compile(
        UUID(proof["repository_id"]), NewDebugReport(failed_run_id=proof["failed_run_id"])
    )
    assert [(item.origin, item.code) for item in report.diagnosis.assertions] == [
        ("runtime_stop", code)
    ]
    assert report.diagnosis.complete_evidence
    assert report.diagnosis.intervention_improvement is None
    if case_id == "D06":
        assert report.diagnosis.candidates[0].action_step == "read-0"
        assert report.diagnosis.observed_anomalies
    else:
        assert report.diagnosis.candidates[0].kind == "unaccepted_model_output"


def test_unfinished_run_is_insufficient_and_bad_reference_or_cross_project_are_rejected(
    history, failed_case
):
    manager, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    other = history.create_repository("未开始定位")
    active = history.create_run(other)
    report = manager.debug_reports.compile(other, NewDebugReport(failed_run_id=str(active)))
    assert (
        report.diagnosis.status == "insufficient_evidence"
        and not report.diagnosis.complete_evidence
    )
    for project, payload, expected in (
        (other, request, ErrorCode.NOT_FOUND),
        (repo, NewDebugReport(failed_run_id=proof["reference_run_id"]), ErrorCode.VALIDATION),
        (
            repo,
            NewDebugReport(
                failed_run_id=proof["failed_run_id"], reference_run_id=proof["failed_run_id"]
            ),
            ErrorCode.VALIDATION,
        ),
    ):
        with pytest.raises(AgentGitError) as error:
            manager.debug_reports.compile(project, payload)
        assert error.value.code == expected


def test_report_metadata_list_is_bounded_readonly_and_project_scoped(
    history, failed_case, monkeypatch
):
    manager, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    ids = [
        manager.create_debug_report(repo, request, f"list-{index}")["debug_report_id"]
        for index in range(3)
    ]
    other = history.create_repository("列表外部项目")

    def forbidden(*args, **kwargs):
        pytest.fail("报告发现列表不读取 CAS 或重新定位")

    monkeypatch.setattr(history.objects, "get", forbidden)
    monkeypatch.setattr(manager.debug_reports, "compile", forbidden)
    first = manager.debug_reports_list(repo, limit=2)
    assert [item["debug_report_id"] for item in first["items"]] == sorted(ids)[:2]
    assert first["has_more"]
    last = manager.debug_reports_list(repo, after_id=UUID(first["next_after_id"]), limit=2)
    assert [item["debug_report_id"] for item in last["items"]] == sorted(ids)[2:]
    assert not last["has_more"] and last["next_after_id"] is None
    for limit in (0, 101, True):
        with pytest.raises(AgentGitError):
            manager.debug_reports_list(repo, limit=limit)
    with pytest.raises(AgentGitError) as error:
        manager.debug_reports_list(other, after_id=UUID(ids[0]))
    assert error.value.code == ErrorCode.NOT_FOUND
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        endpoint = f"/repositories/{repo}/debug-reports"
        response = client.get(endpoint, headers={"X-Repository-ID": str(repo)}, params={"limit": 2})
        assert response.status_code == 200 and response.json() == first
        assert client.get(endpoint, headers={"X-Repository-ID": str(other)}).status_code == 404
        assert (
            client.get(
                endpoint, headers={"X-Repository-ID": str(repo)}, params={"limit": 0}
            ).status_code
            == 422
        )


def test_report_output_bound_rolls_back_index_and_does_not_create_a_job(
    history, failed_case, monkeypatch
):
    manager, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    monkeypatch.setattr("agentgit.services.debug_reports.MAX_REPORT_BYTES", 1)
    with pytest.raises(AgentGitError) as error:
        manager.create_debug_report(repo, request, "too-large")
    assert error.value.code == ErrorCode.OUTPUT_LIMIT
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["debug_reports"])
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 0
        )


def test_evaluation_event_must_match_terminal_state_not_just_be_named_failed(versioned, tmp_path):
    versions, repo, run, commits, environment = versioned
    history = versions.history
    state = AgentState(
        goal=ResearchGoal(text="故障断言交叉核对"),
        status=RunStatus.FAILED,
        error_code="validation_failed",
        evaluation=Evaluation(passed=False, errors=("source_coverage",)),
    )

    def project(connection):
        connection.execute(
            TABLES["runs"].update().where(TABLES["runs"].c.id == run).values(status="failed")
        )

    history.commit(
        repo,
        run,
        expected_head=commits[-1],
        expected_version=3,
        state=state.model_dump(mode="json"),
        environment=environment,
        events=(
            EventInput(
                "run.evaluated",
                "false-evaluation",
                {"evaluation": Evaluation(passed=True).model_dump(mode="json")},
            ),
        ),
        projection=project,
    )
    manager = ManagementService(ResearchService(history, tmp_path / "management"))
    with pytest.raises(AgentGitError) as error:
        manager.debug_reports.compile(repo, NewDebugReport(failed_run_id=str(run)))
    assert error.value.code == ErrorCode.OBJECT_CORRUPT


def test_static_report_migration_roundtrip_preserves_existing_history_schema(history):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["testing"] = True
    with history.engine.begin() as connection:
        config.attributes["connection"] = connection
        assert "debug_reports" in sa.inspect(connection).get_table_names()
        command.downgrade(config, "0015_hybrid_observations")
        assert "debug_reports" not in sa.inspect(connection).get_table_names()
        assert "hybrid_observations" in sa.inspect(connection).get_table_names()
        command.upgrade(config, "head")
        assert "debug_reports" in sa.inspect(connection).get_table_names()
