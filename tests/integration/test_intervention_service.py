"""真实 PostgreSQL 中的目标关系、不可变干预与独立分组执行。"""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from test_recorded_replayer import replay_source as replay_source
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.interventions import NewIntervention
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import BudgetLimits, Plan, PlanNode, ResearchGoal, evolve
from agentgit.services.management import ManagementService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def patch_request(replay_source, *, kind="PROMPT_PATCH", validator_ref=None):
    session, _, commits, _ = replay_source
    base = commits[4 if kind == "TOOL_RESPONSE_PATCH" else 15]
    manager = ManagementService(session.service)
    view = manager.checkout(session.repository_id, base)
    config = session.history.objects.get_json(view["environment"]["input_ref"], ObjectType.BLOB)[
        "config"
    ]
    if validator_ref is None:
        validator_ref = ValidatorService(session.history).freeze(
            session.repository_id, session.state.goal, session.service.corpus
        )
    with session.history.engine.connect() as connection:
        candidates = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == session.run_id
                )
            ).mappings()
        )
        call = next(
            row
            for row in candidates
            if parse_contract(
                session.history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent
            ).checkpoint
            == base
        )
        event = (
            connection.execute(
                sa.select(TABLES["events"]).where(
                    TABLES["events"].c.run_id == session.run_id,
                    TABLES["events"].c.logical_step_id == call["logical_step_id"],
                    TABLES["events"].c.type.in_(("llm.requested", "tool.started")),
                )
            )
            .mappings()
            .one()
        )
    occurrence = parse_contract(
        session.history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
    ).occurrence
    payload = {"kind": "PROMPT_PATCH", "prompt": session.config.prompt + "核验引用。"}
    if kind == "TOOL_RESPONSE_PATCH":
        payload = {"kind": kind, "replacement_call_id": str(call["id"])}
    elif kind == "PLAN_PATCH":
        changed = evolve(
            session.state.plan,
            nodes=tuple(
                evolve(node, key="write-report-verified")
                if node.key == "write-report"
                else evolve(node, dependencies=("write-report-verified",))
                if node.key == "write-sources"
                else node
                for node in session.state.plan.nodes
            ),
        )
        payload = {"kind": kind, "plan": changed.model_dump(mode="json")}
    return manager, {
        "schema_version": 1,
        "source_run_id": str(session.run_id),
        "base_commit": base,
        "target_commit_id": base,
        "target_event_id": event["id"],
        "operator": kind,
        "payload": payload,
        "precondition": {
            "state_ref": view["manifest"]["state_ref"],
            "config_ref": object_digest(ObjectType.BLOB, encode(config)),
            "validator_ref": validator_ref,
            "occurrence": occurrence.model_dump(mode="json"),
        },
        "effect_policy": "test_only" if kind == "TOOL_RESPONSE_PATCH" else "sandbox_or_approval",
        "provenance": {
            "rationale": "固定来源的局部干预",
            "evidence": [{"run_id": str(session.run_id), "seq": event["seq"]}],
        },
    }


@pytest.mark.parametrize("kind", ["PROMPT_PATCH", "PLAN_PATCH"])
def test_typed_patch_is_immutable_idempotent_and_applied_to_new_trial(history, replay_source, kind):
    session, *_ = replay_source
    manager, body = patch_request(replay_source, kind=kind)
    request = NewIntervention.model_validate_json(encode(body))
    result = manager.create_intervention(session.repository_id, request, "prompt-patch")
    assert manager.create_intervention(session.repository_id, request, "prompt-patch") == result
    view = manager.intervention(session.repository_id, UUID(result["intervention_id"]))
    assert view["definition"]["payload_ref"] == result["payload_ref"]
    assert view["definition"]["precondition"] == request.precondition.model_dump(mode="json")
    assert view["payload"] == request.payload.model_dump(mode="json")
    original = history.branch(session.repository_id)
    submitted = manager.create_experiment(
        session.repository_id,
        NewExperiment(
            source_run_id=request.source_run_id,
            from_commit=request.base_commit,
            validator_ref=request.precondition.validator_ref,
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="基线"),
                ExperimentGroup(
                    key="B", label="类型化 Prompt", interventions=(result["intervention_id"],)
                ),
            ),
        ),
        "typed-experiment",
    )
    worker = Worker(manager.jobs)
    assert worker.run_once()["status"] == "completed"
    assert worker.run_once()["status"] == "completed"
    report = manager.experiment(session.repository_id, UUID(submitted["experiment_id"]))
    assert all(trial["verdict"] == "passed" for trial in report["trials"])
    changed = report["trials"][1]
    run = manager.jobs.run(session.repository_id, UUID(changed["run_id"]))
    if kind == "PROMPT_PATCH":
        assert (
            session.service.config(session.repository_id, run["agent_config_id"]).prompt
            == request.payload.prompt
        )
    else:
        assert history.restore(session.repository_id, changed["checkpoint_commit_id"]).data[
            "plan"
        ] == request.payload.plan.model_dump(mode="json")
    assert history.branch(session.repository_id) == original
    with history.engine.begin() as connection, pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(TABLES["interventions"].delete())


@pytest.mark.parametrize("field", ["state_ref", "config_ref", "occurrence"])
def test_stale_preconditions_rejected_without_publishing_intervention(
    history, replay_source, field
):
    session, *_ = replay_source
    manager, body = patch_request(replay_source)
    if field == "occurrence":
        body["precondition"][field]["request_digest"] = "0" * 64
    else:
        body["precondition"][field] = "0" * 64
    with pytest.raises(AgentGitError) as caught:
        manager.create_intervention(
            session.repository_id,
            NewIntervention.model_validate_json(encode(body)),
            "stale-" + field,
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["interventions"])
            ).scalar_one()
            == 0
        )


def test_target_relationship_cross_project_and_wrong_validator_rejected(history, replay_source):
    session, _, commits, _ = replay_source
    manager, body = patch_request(replay_source)
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError) as caught:
        manager.create_intervention(
            other, NewIntervention.model_validate_json(encode(body)), "cross-project"
        )
    assert caught.value.code == ErrorCode.NOT_FOUND
    bad = {**body, "target_commit_id": commits[17]}
    with pytest.raises(AgentGitError) as caught:
        manager.create_intervention(
            session.repository_id, NewIntervention.model_validate_json(encode(bad)), "wrong-target"
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    wrong = ValidatorService(history).freeze(
        session.repository_id, ResearchGoal(text="其他任务"), session.service.corpus
    )
    bad = {**body, "precondition": {**body["precondition"], "validator_ref": wrong}}
    with pytest.raises(AgentGitError) as caught:
        manager.create_intervention(
            session.repository_id,
            NewIntervention.model_validate_json(encode(bad)),
            "wrong-validator",
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH


def test_plan_cannot_remove_completed_nodes_or_add_unapproved_tools(history, replay_source):
    session, *_ = replay_source
    manager, body = patch_request(replay_source)
    for plan, code in (
        (Plan(nodes=(PlanNode(key="new", kind="search"),)), ErrorCode.PLAN_INVALID),
        (
            Plan(
                nodes=tuple(
                    node
                    if node.key != "write-report"
                    else PlanNode(
                        key=node.key, kind="terminal_exec", dependencies=node.dependencies
                    )
                    for node in session.state.plan.nodes
                )
            ),
            ErrorCode.FORBIDDEN,
        ),
    ):
        changed = {
            **body,
            "operator": "PLAN_PATCH",
            "payload": {"kind": "PLAN_PATCH", "plan": plan.model_dump(mode="json")},
        }
        with pytest.raises(AgentGitError) as caught:
            manager.create_intervention(
                session.repository_id,
                NewIntervention.model_validate_json(encode(changed)),
                "bad-plan-" + code,
            )
        assert caught.value.code == code


def test_tool_observation_patch_is_frozen_but_requires_hybrid_execution(history, replay_source):
    session, *_ = replay_source
    manager, body = patch_request(replay_source, kind="TOOL_RESPONSE_PATCH")
    before = manager.validator(session.repository_id, body["precondition"]["validator_ref"])
    request = NewIntervention.model_validate_json(encode(body))
    result = manager.create_intervention(session.repository_id, request, "observation-patch")
    stored = manager.intervention(session.repository_id, UUID(result["intervention_id"]))
    assert stored["definition"]["effect_policy"] == "test_only"
    assert stored["definition"]["replacement_response_ref"]
    with pytest.raises(AgentGitError) as caught:
        manager.create_experiment(
            session.repository_id,
            NewExperiment(
                source_run_id=request.source_run_id,
                from_commit=request.base_commit,
                validator_ref=request.precondition.validator_ref,
                repeats=1,
                budget=BudgetLimits(),
                groups=(
                    ExperimentGroup(key="A", label="A"),
                    ExperimentGroup(key="B", label="B", interventions=(result["intervention_id"],)),
                ),
            ),
            "needs-hybrid",
        )
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert manager.validator(session.repository_id, request.precondition.validator_ref) == before
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["experiments"])
            ).scalar_one()
            == 0
        )


def test_api_cli_share_intervention_idempotency_and_project_boundary(
    history, replay_source, tmp_path, monkeypatch
):
    session, *_ = replay_source
    manager, body = patch_request(replay_source)
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    token = "fixture-intervention-token-0000000000000000000000000"
    with TestClient(
        create_app(Settings(api_token=SecretStr(token)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(session.repository_id)},
    ) as client:
        context_parameters = {
            "source_run_id": body["source_run_id"],
            "from_commit": body["base_commit"],
            "target_event_id": body["target_event_id"],
            "validator_ref": body["precondition"]["validator_ref"],
        }
        context = client.get("/intervention-context", params=context_parameters)
        assert context.status_code == 200, context.text
        assert context.json()["precondition"] == NewIntervention.model_validate_json(
            encode(body)
        ).precondition.model_dump(mode="json")
        assert context.json()["target_commit_id"] == body["target_commit_id"]
        assert context.json()["config"] == session.config.model_dump(mode="json")
        cli_context = CliRunner().invoke(
            app,
            [
                "intervention-context",
                str(session.repository_id),
                "--source-run",
                body["source_run_id"],
                "--from-commit",
                body["base_commit"],
                "--target-event-id",
                str(body["target_event_id"]),
                "--validator-ref",
                body["precondition"]["validator_ref"],
            ],
        )
        assert cli_context.exit_code == 0, (cli_context.stdout, cli_context.exception)
        assert json.loads(cli_context.stdout) == context.json()
        response = client.post(
            "/interventions", json=body, headers={"Idempotency-Key": "shared-patch"}
        )
        assert response.status_code == 201, response.text
        path = tmp_path / "patch.json"
        path.write_bytes(encode(body))
        cli = CliRunner().invoke(
            app,
            [
                "intervention",
                str(session.repository_id),
                "--file",
                str(path),
                "--key",
                "shared-patch",
            ],
        )
        assert cli.exit_code == 0, (cli.stdout, cli.exception)
        assert json.loads(cli.stdout) == response.json()
        identity = response.json()["intervention_id"]
        assert client.get(f"/interventions/{identity}").status_code == 200
        other = history.create_repository("不可访问干预")
        assert (
            client.get(
                "/intervention-context",
                params=context_parameters,
                headers={"X-Repository-ID": str(other)},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/interventions/{identity}", headers={"X-Repository-ID": str(other)}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/interventions",
                json={**body, "permissions": ["all"]},
                headers={"Idempotency-Key": "forbidden"},
            ).status_code
            == 422
        )


@pytest.mark.parametrize("provider", ["tool:source_read/v1", "tool:artifact_write/v1"])
def test_replacement_requires_same_request_signature_and_readonly_effect(
    history, replay_source, provider
):
    session, *_ = replay_source
    manager, body = patch_request(replay_source, kind="TOOL_RESPONSE_PATCH")
    target = body["payload"]["replacement_call_id"]
    with history.engine.connect() as connection:
        other = connection.execute(
            sa.select(TABLES["external_calls"].c.id)
            .where(
                TABLES["external_calls"].c.run_id == session.run_id,
                TABLES["external_calls"].c.provider == provider,
                TABLES["external_calls"].c.id != UUID(target),
            )
            .limit(1)
        ).scalar_one()
    body["payload"]["replacement_call_id"] = str(other)
    with pytest.raises(AgentGitError) as caught:
        manager.create_intervention(
            session.repository_id,
            NewIntervention.model_validate_json(encode(body)),
            "bad-replacement",
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH


def test_confirmation_observations_cannot_be_borrowed_into_screening_patch(history, replay_source):
    session, *_ = replay_source
    manager, body = patch_request(replay_source, kind="TOOL_RESPONSE_PATCH")
    confirm = ValidatorService(history).freeze(
        session.repository_id, session.state.goal, session.service.corpus, fixture_split="confirm"
    )
    result = manager.create_experiment(
        session.repository_id,
        NewExperiment(
            source_run_id=str(session.run_id),
            from_commit=body["base_commit"],
            validator_ref=confirm,
            repeats=1,
            budget=BudgetLimits(),
            groups=(
                ExperimentGroup(key="A", label="确认 A"),
                ExperimentGroup(key="B", label="确认 B"),
            ),
        ),
        "confirm-observations",
    )
    worker = Worker(manager.jobs)
    assert worker.run_once()["status"] == "completed"
    assert worker.run_once()["status"] == "completed"
    with history.engine.connect() as connection:
        replacement = connection.execute(
            sa.select(TABLES["external_calls"].c.id).where(
                TABLES["external_calls"].c.run_id == UUID(result["trials"][0]["run_id"]),
                TABLES["external_calls"].c.logical_step_id
                == body["precondition"]["occurrence"]["logical_step_id"],
            )
        ).scalar_one()
    body["payload"]["replacement_call_id"] = str(replacement)
    with pytest.raises(AgentGitError) as caught:
        manager.create_intervention(
            session.repository_id,
            NewIntervention.model_validate_json(encode(body)),
            "leaked-holdout",
        )
    assert caught.value.code == ErrorCode.FORBIDDEN
