"""真实 S7 Job 构造缺失产物故障，再核验静态候选与独立修复试验。"""

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from diagnosis_evidence import pack_repository_evidence
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_intervention_service import patch_request
from test_management_api import TOKEN
from test_recorded_replayer import replay_source as replay_source
from typer.testing import CliRunner

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.interventions import NewIntervention
from agentgit.core.research import BudgetLimits, evolve
from agentgit.services.management import public
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


@pytest.fixture
def candidate_case(history, replay_source):
    session, *_ = replay_source
    manager, body = patch_request(replay_source, kind="PLAN_PATCH")
    repo = session.repository_id
    good = session.state.plan
    bad = evolve(good, nodes=tuple(node for node in good.nodes if node.key != "write-sources"))
    body["payload"]["plan"] = bad.model_dump(mode="json")
    injected = manager.create_intervention(
        repo, NewIntervention.model_validate_json(encode(body)), "omit-sources"
    )
    constructed = manager.create_experiment(
        repo,
        NewExperiment(
            source_run_id=body["source_run_id"],
            from_commit=body["base_commit"],
            validator_ref=body["precondition"]["validator_ref"],
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="正常对照"),
                ExperimentGroup(
                    key="B", label="省略来源文件", interventions=(injected["intervention_id"],)
                ),
            ),
        ),
        "construct-failure",
    )
    worker = Worker(manager.jobs)
    assert {worker.run_once()["status"] for _ in range(2)} == {"completed", "failed"}
    experiment = manager.experiment(repo, UUID(constructed["experiment_id"]))
    failed = next(item for item in experiment["trials"] if item["group"] == "B")
    assert failed["verdict"] == "failed"
    created = manager.create_debug_report(
        repo,
        NewDebugReport(failed_run_id=failed["run_id"], reference_run_id=str(session.run_id)),
        "report-omission",
    )
    identity = UUID(created["debug_report_id"])
    stored = manager.debug_report(repo, identity)
    candidate = stored["report"]["diagnosis"]["candidates"][0]
    # 已完成的读取来自父 Run，静态图保留范围缺口，不能冒称完整接受证据。
    assert candidate["kind"] == "unaccepted_model_output"
    assert candidate["uncertainties"]
    event_id = candidate["event"]["event_id"]
    validator = body["precondition"]["validator_ref"]
    bound = manager.debug_candidate_context(repo, identity, event_id, validator)
    context = bound["context"]
    repair = manager.create_intervention(
        repo,
        NewIntervention.model_validate_json(
            encode(
                {
                    **{
                        key: context[key]
                        for key in (
                            "source_run_id",
                            "base_commit",
                            "target_commit_id",
                            "target_event_id",
                            "precondition",
                        )
                    },
                    "operator": "PLAN_PATCH",
                    "payload": {"kind": "PLAN_PATCH", "plan": good.model_dump(mode="json")},
                    "effect_policy": "sandbox_or_approval",
                    "provenance": {
                        "rationale": "在候选决策前补回 write-sources，保留已完成节点和固定验收器。",
                        "evidence": [
                            {
                                "run_id": candidate["event"]["run_id"],
                                "seq": candidate["event"]["seq"],
                            }
                        ],
                    },
                }
            )
        ),
        "repair-sources",
    )
    request = NewExperiment(
        source_run_id=context["source_run_id"],
        from_commit=context["base_commit"],
        validator_ref=validator,
        repeats=5,
        budget=BudgetLimits(),
        groups=(
            ExperimentGroup(key="A", label="原失败计划"),
            ExperimentGroup(
                key="B", label="补回来源文件", interventions=(repair["intervention_id"],)
            ),
        ),
    )
    return session, manager, identity, event_id, bound, request


def test_candidate_real_ab_five_trials_and_raw_evidence(
    history, candidate_case, tmp_path, monkeypatch
):
    session, manager, identity, event_id, bound, request = candidate_case
    repo = session.repository_id
    original = manager.debug_report(repo, identity)
    branch = history.branch(repo)
    files = session.workspace.contents()
    created = manager.create_experiment(repo, request, "candidate-five")
    experiment_id = UUID(created["experiment_id"])
    pending = manager.debug_candidate_experiment(repo, identity, event_id, experiment_id)
    assert pending["effect_status"] == "incomplete" and pending["success_rate_difference"] is None
    assert len(pending["experiment"]["trials"]) == 10
    worker = Worker(manager.jobs)
    statuses = [worker.run_once()["status"] for _ in range(10)]
    assert statuses.count("failed") == statuses.count("completed") == 5
    assert worker.run_once() is None
    with history.engine.connect() as connection:
        refs_before = connection.execute(
            sa.select(sa.func.count()).select_from(TABLES["object_refs"])
        ).scalar_one()

    def forbidden(*args, **kwargs):
        pytest.fail("已完成实验的候选关联查询也不得写 CAS 或执行")

    with monkeypatch.context() as readonly:
        readonly.setattr(history.objects, "put", forbidden)
        readonly.setattr(MockResearchModel, "invoke", forbidden)
        readonly.setattr(ToolRegistry, "invoke", forbidden)
        readonly.setattr(Worker, "run_once", forbidden)
        observed = manager.debug_candidate_experiment(repo, identity, event_id, experiment_id)
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["object_refs"])
            ).scalar_one()
            == refs_before
        )
    assert observed["effect_status"] == "assessed"
    assert observed["success_rate_difference"] == {"numerator": "1", "denominator": "1"}
    assert observed["experiment"]["groups"] == [
        {"key": "A", "total": 5, "passed": 0, "incomplete": 0},
        {"key": "B", "total": 5, "passed": 5, "incomplete": 0},
    ]
    trials = observed["experiment"]["trials"]
    assert (
        len({item["run_id"] for item in trials}) == len({item["job_id"] for item in trials}) == 10
    )
    for trial in trials:
        assert trial["assessment"]["validator_ref"] == request.validator_ref
        assert trial["replay"]["actual"]["unknown_outcomes"] == 0
        assert trial["verdict"] == ("failed" if trial["group"] == "A" else "passed")
        run = manager.jobs.run(repo, UUID(trial["run_id"]))
        assert str(run["origin_run_id"]) == request.source_run_id
        assert run["base_commit_id"] == request.from_commit
    assert manager.debug_report(repo, identity) == original
    assert original["report"]["diagnosis"]["intervention_improvement"] is None
    assert history.branch(repo) == branch and session.workspace.contents() == files
    proof = {
        "case_id": "S8-illustrative-plan-repair",
        "repository_id": str(repo),
        "failed_run_id": original["report"]["failed"]["run_id"],
        "reference_run_id": str(session.run_id),
        "graph": original["report"]["failed"],
        "reference_graph": original["report"]["reference"],
    }
    bundle = pack_repository_evidence(history, proof)
    with history.engine.connect() as connection:
        for name in ("debug_reports", "interventions", "experiments", "experiment_trials", "jobs"):
            table = TABLES[name]
            rows = list(
                connection.execute(sa.select(table).where(table.c.repository_id == repo)).mappings()
            )
            assert len(rows) <= 1000
            bundle["indexes"][name] = [public(dict(row)) for row in rows]
    destination = os.environ.get("AGENTGIT_DEBUG_EXPERIMENT_EVIDENCE_DIR")
    path = Path(destination).resolve() if destination else tmp_path / "archive"
    if destination:
        assert path.name.startswith("S8-candidate-experiments-") and path.is_relative_to(
            Path.cwd() / ".agentgit/evidence"
        )
        assert not path.exists()
    path.mkdir(parents=True)
    for name, content in (
        ("effect.json", observed),
        ("static-report.json", original),
        ("evidence.json", bundle),
    ):
        (path / name).write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


def test_candidate_context_and_effect_are_readonly_scoped_api_cli(
    history, candidate_case, monkeypatch
):
    session, manager, identity, event_id, bound, request = candidate_case
    repo = session.repository_id
    created = manager.create_experiment(repo, evolve(request, repeats=1), "readonly-candidate")
    other = history.create_repository("报告外部项目")
    names = (
        "events",
        "commits",
        "jobs",
        "runs",
        "external_calls",
        "interventions",
        "experiments",
        "object_refs",
    )

    def counts():
        with history.engine.connect() as connection:
            return {
                name: connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES[name])
                ).scalar_one()
                for name in names
            }

    before = counts()

    def forbidden(*args, **kwargs):
        pytest.fail("只读候选查询不得执行或写 CAS")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    monkeypatch.setattr(Worker, "run_once", forbidden)
    monkeypatch.setattr(history.objects, "put", forbidden)

    @contextmanager
    def current(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", current)
    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        endpoint = f"/debug-reports/{identity}/candidate-context"
        params = {"candidate_event_id": event_id, "validator_ref": request.validator_ref}
        assert client.get(endpoint, params=params).json() == bound
        assert client.get(endpoint, params={**params, "candidate_event_id": 0}).status_code == 422
        assert client.get(endpoint, params={**params, "validator_ref": "x"}).status_code == 422
        assert (
            client.get(endpoint, params=params, headers={"X-Repository-ID": str(other)}).status_code
            == 404
        )
        effect = client.get(
            f"/debug-reports/{identity}/candidate-experiment",
            params={"candidate_event_id": event_id, "experiment_id": created["experiment_id"]},
        )
        assert effect.status_code == 200 and effect.json()["success_rate_difference"] is None
    cli = CliRunner().invoke(
        app,
        [
            "debug-candidate-context",
            str(repo),
            str(identity),
            "--candidate-event-id",
            str(event_id),
            "--validator-ref",
            request.validator_ref,
        ],
    )
    assert cli.exit_code == 0 and json.loads(cli.stdout) == bound
    cli = CliRunner().invoke(
        app,
        [
            "debug-candidate-experiment",
            str(repo),
            str(identity),
            "--candidate-event-id",
            str(event_id),
            "--experiment-id",
            created["experiment_id"],
        ],
    )
    assert cli.exit_code == 0 and json.loads(cli.stdout)["effect_status"] == "incomplete"
    assert counts() == before


def test_wrong_candidate_source_or_multivariable_experiment_cannot_claim_effect(
    history, candidate_case, replay_source
):
    session, manager, identity, event_id, bound, request = candidate_case
    repo = session.repository_id
    altered = evolve(
        request,
        repeats=1,
        groups=(
            ExperimentGroup(
                key="A", label="同时改 Prompt", prompt=session.config.prompt + "再次核验。"
            ),
            request.groups[1],
        ),
    )
    created = manager.create_experiment(repo, altered, "two-variables")
    with pytest.raises(AgentGitError) as error:
        manager.debug_candidate_experiment(repo, identity, event_id, UUID(created["experiment_id"]))
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
    with pytest.raises(AgentGitError) as error:
        manager.debug_candidate_context(
            repo, identity, bound["request_event"]["event_id"], request.validator_ref
        )
    assert error.value.code == ErrorCode.NOT_FOUND
    other = history.create_repository("跨项目候选")
    with pytest.raises(AgentGitError) as error:
        manager.debug_candidate_experiment(
            other, identity, event_id, UUID(created["experiment_id"])
        )
    assert error.value.code == ErrorCode.NOT_FOUND
    _, body = patch_request(replay_source, validator_ref=request.validator_ref)
    wrong_source = manager.create_experiment(
        repo,
        evolve(
            request,
            source_run_id=body["source_run_id"],
            from_commit=body["base_commit"],
            repeats=1,
            groups=(
                ExperimentGroup(key="A", label="其他来源 A"),
                ExperimentGroup(key="B", label="其他来源 B"),
            ),
        ),
        "wrong-source",
    )
    with pytest.raises(AgentGitError) as error:
        manager.debug_candidate_experiment(
            repo, identity, event_id, UUID(wrong_source["experiment_id"])
        )
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_current_recording_cannot_replace_frozen_candidate_observation(candidate_case, monkeypatch):
    session, manager, identity, event_id, bound, request = candidate_case
    service = manager.experiments.interventions
    original = service.recording(session.repository_id, UUID(bound["context"]["source_call_id"]))
    monkeypatch.setattr(
        service,
        "recording",
        lambda *args: replace(original, saved=evolve(original.saved, response_ref="f" * 64)),
    )
    with pytest.raises(AgentGitError) as error:
        manager.debug_candidate_context(
            session.repository_id, identity, event_id, request.validator_ref
        )
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_exhausted_trials_keep_effect_unknown(candidate_case):
    session, manager, identity, event_id, _, request = candidate_case
    repo = session.repository_id
    created = manager.create_experiment(
        repo, evolve(request, repeats=1, budget=BudgetLimits(max_model_calls=1)), "candidate-budget"
    )
    worker = Worker(manager.jobs)
    assert all(worker.run_once()["status"] == "budget_exhausted" for _ in range(2))
    observed = manager.debug_candidate_experiment(
        repo, identity, event_id, UUID(created["experiment_id"])
    )
    assert observed["effect_status"] == "incomplete" and observed["success_rate_difference"] is None
    assert all(item["verdict"] == "incomplete" for item in observed["experiment"]["trials"])
