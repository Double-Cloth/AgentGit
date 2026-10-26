"""真实筛选前预注册、锁后开启、项目归属和事务恢复；不开模型确认。"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from test_management_api import TOKEN
from test_repair_studies import drive_study
from test_repair_worker import create_case, drive
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_pools import NewRepairValidationSuite
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.repair_validation import RepairValidationPlan
from agentgit.core.research import ResearchGoal, evolve
from agentgit.repair.verification import verify_repair
from agentgit.services.management import ManagementService
from agentgit.services.repair_pools import TABLES, RepairValidationPoolService
from agentgit.services.repair_studies import RepairStudyService

pytestmark = pytest.mark.integration


def setup_pool(history, tmp_path, *, budget_transform=lambda value: value, adapter_binding=None):
    repo, search, research, _ = create_case(
        history,
        tmp_path,
        mode="prompt",
        budget_transform=budget_transform,
        adapter_binding=adapter_binding,
    )
    config = research.create_config(repo)
    origins = []
    for index in range(4):
        task = research.create_task(
            repo, config, ResearchGoal(text=f"固定回归任务 {index}：比较三个框架")
        )
        session = research.start(repo, task)
        assert session.execute().status.value == "completed"
        with history.engine.connect() as connection:
            commits = TABLES["run_commits"]
            first = connection.execute(
                sa.select(commits.c.commit_id)
                .where(
                    commits.c.repository_id == repo,
                    commits.c.run_id == session.run_id,
                )
                .order_by(commits.c.run_order)
                .limit(1)
            ).scalar_one()
        origins.append(dict(schema_version=1, source_run_id=str(session.run_id), from_commit=first))
    request = NewRepairValidationSuite.model_validate_json(
        encode(
            dict(
                schema_version=1,
                search_id=str(search),
                regression=origins,
            )
        )
    )
    return repo, search, research, RepairValidationPoolService(research), request


def locked(history, tmp_path):
    repo, search, research, pools, request = setup_pool(history, tmp_path)
    made = pools.submit(repo, request, actor="operator", key="suite")
    source = drive(research, repo, search)
    assert source["status"] == "needs_confirmation"
    study = RepairStudyService(history).submit(
        repo,
        NewRepairStudy(search_id=str(search), field_policy=None),
        actor="operator",
        key="study",
    )
    result = drive_study(research, repo)
    return repo, UUID(study["study_id"]), UUID(made["suite_id"]), pools, request, result


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in (
                "repair_validation_suites",
                "repair_validation_samples",
                "repair_validation_openings",
                "repair_study_events",
                "jobs",
                "runs",
            )
        }


def test_actual_screen_reduce_then_open_preserves_lock_and_creates_no_confirmation_job(
    history, tmp_path, monkeypatch
):
    repo, study, suite, pools, request, result = locked(history, tmp_path)
    before = counts(history)
    original_lock = result["final_lock"]

    def forbidden(*args, **kwargs):
        pytest.fail("注册/开启/读取不能调用模型或工具")

    with monkeypatch.context() as no_execution:
        no_execution.setattr(MockResearchModel, "invoke", forbidden)
        no_execution.setattr(ToolRegistry, "invoke", forbidden)
        opened = pools.open(repo, study, suite, actor="operator", key="open")
        assert (
            opened["opened"]
            and pools.open(repo, study, suite, actor="operator", key="open") == opened
        )
        assert pools.submit(repo, request, actor="operator", key="suite")["suite_id"] == str(suite)
    current = RepairStudyService(history).view(repo, study)
    assert current["status"] == "validation_ready" and current["final_lock"] == original_lock
    assert current["usage"]["started"]["confirm"] == current["usage"]["started"]["regression"] == 0
    after = counts(history)
    assert after["jobs"] == before["jobs"] and after["runs"] == before["runs"]
    assert after["repair_validation_openings"] == 1
    assert after["repair_study_events"] == before["repair_study_events"] + 1
    with monkeypatch.context() as readonly:
        readonly.setattr(history.objects, "put", forbidden)
        value = pools.opening(repo, study)
        frozen = pools.view(repo, suite)
    plan = RepairValidationPlan.model_validate_json(encode(value["opening"]["plan"]))
    summary = verify_repair(plan, ())
    assert summary.status == "needs_review" and summary.confirm_attempted == 0
    assert summary.regression_attempted == 0 and not summary.production_eligible
    assert len(plan.pool.confirm) == 6 and len(plan.pool.regression) == 4
    assert all(
        v["ticket"]["response_status"] == "not_generated" for v in frozen["manifest"]["confirm"]
    )
    path = os.environ.get("AGENTGIT_S10_POOL_EVIDENCE")
    if path:
        from diagnosis_evidence import pack_repository_evidence

        from agentgit.services.diagnosis import BehaviorService
        from agentgit.services.management import public

        source_run = UUID(current["manifest"]["initial_state"]["screening_run_ids"][0])
        graph = BehaviorService(history).trace(repo, source_run).model_dump(mode="json")
        evidence = dict(
            repository_id=str(repo),
            failed_run_id=str(source_run),
            reference_run_id=str(source_run),
            reference_role="source_alias_for_archive_only",
            case_id="S10-preregistered-pool/v1",
            graph=graph,
            reference_graph=graph,
        )
        raw = pack_repository_evidence(history, evidence)
        with history.engine.connect() as connection:
            indexes = {
                name: [
                    public(dict(row))
                    for row in connection.execute(sa.select(TABLES[name])).mappings()
                ]
                for name in (
                    "repair_validation_suites",
                    "repair_validation_samples",
                    "repair_validation_openings",
                    "repair_study_locks",
                    "repair_study_events",
                )
            }
        with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(
                    schema_version=1,
                    pool=frozen,
                    opening=value,
                    study=current,
                    verification=summary.model_dump(mode="json"),
                    indexes=indexes,
                    evidence=evidence,
                    raw_repository=raw,
                ),
                handle,
                ensure_ascii=False,
                indent=2,
            )


def test_registration_requires_untouched_search_and_distinct_tasks(history, tmp_path):
    repo, search, research, pools, request = setup_pool(history, tmp_path)
    duplicate_tasks = deepcopy(request.model_dump(mode="json"))
    # 不同运行但同一任务正文也不能当成四个固定回归任务。
    task = research.create_task(
        repo, research.create_config(repo), ResearchGoal(text="固定回归任务 0：比较三个框架")
    )
    session = research.start(repo, task)
    session.execute()
    with history.engine.connect() as connection:
        links = TABLES["run_commits"]
        first = connection.execute(
            sa.select(links.c.commit_id)
            .where(links.c.run_id == session.run_id)
            .order_by(links.c.run_order)
            .limit(1)
        ).scalar_one()
    duplicate_tasks["regression"][1] = dict(
        schema_version=1, source_run_id=str(session.run_id), from_commit=first
    )
    with pytest.raises(AgentGitError) as error:
        pools.submit(
            repo,
            NewRepairValidationSuite.model_validate_json(encode(duplicate_tasks)),
            actor="operator",
            key="duplicate",
        )
    assert (
        error.value.code == ErrorCode.VALIDATION
        and counts(history)["repair_validation_suites"] == 0
    )
    lease = pools.screening.claim("started", repository=repo)
    assert lease is not None
    with pytest.raises(AgentGitError) as error:
        pools.submit(repo, request, actor="operator", key="late")
    assert (
        error.value.code == ErrorCode.FORBIDDEN
        and counts(history)["repair_validation_samples"] == 0
    )


@pytest.mark.parametrize("point", ["before_registration_commit", "after_registration_commit"])
def test_registration_commit_failure_is_atomic_or_idempotent(history, tmp_path, point):
    repo, _, _, pools, request = setup_pool(history, tmp_path)

    def fault(stage):
        if stage == point:
            raise RuntimeError("注册提交边界中断")

    pools.fault = fault
    with pytest.raises(RuntimeError):
        pools.submit(repo, request, actor="operator", key="once")
    assert counts(history)["repair_validation_samples"] == (0 if point.startswith("before") else 10)
    pools.fault = lambda _: None
    made = pools.submit(repo, request, actor="operator", key="once")
    assert pools.submit(repo, request, actor="operator", key="once") == made
    assert (
        counts(history)["repair_validation_suites"] == 1
        and counts(history)["repair_validation_samples"] == 10
    )


@pytest.mark.parametrize("point", ["before_opening_commit", "after_opening_commit"])
def test_opening_event_plan_and_control_commit_together(history, tmp_path, point):
    repo, study, suite, pools, _, _ = locked(history, tmp_path)
    before = counts(history)

    def fault(stage):
        if stage == point:
            raise RuntimeError("开启提交边界中断")

    pools.fault = fault
    with pytest.raises(RuntimeError):
        pools.open(repo, study, suite, actor="operator", key="once")
    if point.startswith("before"):
        assert counts(history) == before and pools.opening(repo, study) is None
        assert RepairStudyService(history).view(repo, study)["status"] == "awaiting_samples"
    pools.fault = lambda _: None
    made = pools.open(repo, study, suite, actor="operator", key="once")
    assert pools.open(repo, study, suite, actor="operator", key="once") == made
    assert counts(history)["repair_study_events"] == before["repair_study_events"] + 1


def test_single_connection_six_openings_publish_one_and_cancellation_preserves_binding(
    history, tmp_path
):
    repo, study, suite, pools, _, _ = locked(history, tmp_path)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    engine = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    from agentgit.services.history import HistoryService
    from agentgit.services.research import ResearchService

    limited_history = HistoryService(engine, history.objects)
    limited = RepairValidationPoolService(
        ResearchService(limited_history, pools.research.data_root)
    )
    try:
        with ThreadPoolExecutor(max_workers=6) as workers:
            values = list(
                workers.map(
                    lambda _: limited.open(repo, study, suite, actor="operator", key="six"),
                    range(6),
                )
            )
        assert (
            all(v == values[0] for v in values)
            and counts(history)["repair_validation_openings"] == 1
        )
        prior = limited.opening(repo, study)
        RepairStudyService(limited_history).cancel(repo, study, actor="operator")
        assert limited.open(repo, study, suite, actor="operator", key="six") == values[0]
        assert limited.opening(repo, study) == prior
        assert RepairStudyService(limited_history).view(repo, study)["status"] == "cancelled"
    finally:
        engine.dispose()


def test_api_rejects_uploaded_results_foreign_suite_and_history_rewrite(
    history, tmp_path, monkeypatch
):
    repo, search, research, pools, request = setup_pool(history, tmp_path)
    manager = ManagementService(research)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        body = request.model_dump(mode="json")
        assert (
            client.post(
                "/repair-validation-suites",
                json={**body, "passed": True},
                headers={"Idempotency-Key": "bad"},
            ).status_code
            == 422
        )
        response = client.post(
            "/repair-validation-suites", json=body, headers={"Idempotency-Key": "suite"}
        )
        assert response.status_code == 201, response.text
        suite = UUID(response.json()["suite_id"])

        @contextmanager
        def current(testing):
            yield manager

        monkeypatch.setattr("agentgit.cli.main.management", current)
        request_file = tmp_path / "suite.json"
        request_file.write_bytes(encode(body))
        repeated = CliRunner().invoke(
            app,
            ["repair-validation-suite", str(repo), "--file", str(request_file), "--key", "suite"],
        )
        assert repeated.exit_code == 0 and json.loads(repeated.output) == response.json()
        assert (
            client.get(
                f"/repair-validation-suites/{suite}", headers={"X-Repository-ID": str(uuid4())}
            ).status_code
            == 404
        )
        drive(research, repo, search)
        study = RepairStudyService(history).submit(
            repo,
            NewRepairStudy(search_id=str(search), field_policy=None),
            actor="operator",
            key="study",
        )
        identity = UUID(study["study_id"])
        assert (
            client.post(
                f"/repair-studies/{identity}/open-validation",
                json={"schema_version": 1, "suite_id": str(suite)},
                headers={"Idempotency-Key": "early"},
            ).status_code
            == 403
        )
        drive_study(research, repo)
        opened = client.post(
            f"/repair-studies/{identity}/open-validation",
            json={"schema_version": 1, "suite_id": str(suite)},
            headers={"Idempotency-Key": "open"},
        )
        assert opened.status_code == 200 and opened.json()["opened"], opened.text
        assert (
            client.get(f"/repair-studies/{identity}/validation").json()["validation"]["plan_ref"]
            == opened.json()["plan_ref"]
        )
        repeated = CliRunner().invoke(
            app, ["repair-validation-open", str(repo), str(identity), str(suite), "--key", "open"]
        )
        assert repeated.exit_code == 0 and json.loads(repeated.output) == opened.json()
        viewed = CliRunner().invoke(app, ["repair-validation-status", str(repo), str(identity)])
        assert (
            viewed.exit_code == 0
            and json.loads(viewed.output)["validation"]["plan_ref"] == opened.json()["plan_ref"]
        )
    for name in (
        "repair_validation_suites",
        "repair_validation_samples",
        "repair_validation_openings",
    ):
        with history.engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(TABLES[name].delete())


def test_cancel_before_opening_preserves_pool_and_database_cannot_fake_ready(history, tmp_path):
    repo, study, suite, pools, _, _ = locked(history, tmp_path)
    with history.engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            TABLES["repair_study_jobs"]
            .update()
            .where(
                TABLES["repair_study_jobs"].c.study_id == study,
            )
            .values(status="validation_ready", revision=TABLES["repair_study_jobs"].c.revision + 1)
        )
    RepairStudyService(history).cancel(repo, study, actor="operator")
    before = counts(history)
    with pytest.raises(AgentGitError) as error:
        pools.open(repo, study, suite, actor="operator", key="cancelled")
    assert error.value.code == ErrorCode.FORBIDDEN and counts(history) == before
    assert pools.opening(repo, study) is None


def test_insufficient_frozen_confirmation_quota_does_not_open_or_expand_budget(history, tmp_path):
    repo, search, research, pools, request = setup_pool(
        history,
        tmp_path,
        budget_transform=lambda budget: evolve(budget, quotas=evolve(budget.quotas, confirm=5)),
    )
    made = pools.submit(repo, request, actor="operator", key="suite")
    drive(research, repo, search)
    study = RepairStudyService(history).submit(
        repo,
        NewRepairStudy(search_id=str(search), field_policy=None),
        actor="operator",
        key="study",
    )
    identity = UUID(study["study_id"])
    drive_study(research, repo)
    before = counts(history)
    result = pools.open(
        repo, identity, UUID(made["suite_id"]), actor="operator", key="insufficient"
    )
    assert not result["opened"] and result["decision"]["reason"] == "phase_trials"
    assert counts(history) == before and pools.opening(repo, identity) is None
    assert RepairStudyService(history).view(repo, identity)["status"] == "awaiting_samples"
