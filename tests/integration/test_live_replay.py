"""Live 从安全历史独立执行，保留累计用量并只扣新增预算。"""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_recorded_replayer import replay_source as replay_source
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, BudgetLimits, PlanNode, evolve
from agentgit.core.versions import ArtifactPatch, LiveOverrides, LiveReplay, RecordedReplay
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tools", [1, 16])
def test_live_new_budget_does_not_charge_source_and_never_reuses_source_calls(
    history, replay_source, tools
):
    session, final, commits, _ = replay_source
    original = history.branch(session.repository_id)
    source = commits[8]
    before = parse_contract(encode(history.restore(session.repository_id, source).data), AgentState)
    assert before.usage.tool_calls > 1
    manager = ManagementService(session.service)
    request = LiveReplay(
        source_run_id=str(session.run_id),
        from_commit=source,
        branch_name="live-fresh",
        budget=BudgetLimits(max_tool_calls=tools),
    )
    submitted = manager.replay(session.repository_id, request, "live-fresh")
    assert manager.replay(session.repository_id, request, "live-fresh") == submitted
    run, job = UUID(submitted["run_id"]), UUID(submitted["job_id"])
    assert Worker(manager.jobs).run_once()["status"] == (
        "budget_exhausted" if tools == 1 else "completed"
    )
    report = manager.replay_status(session.repository_id, job)["replay"]
    assert report["mode"] == "live" and report["initialized"]
    assert report["budget_baseline"] == before.usage.model_dump(mode="json")
    assert report["new_usage"]["tool_calls"] == (1 if tools == 1 else 6)
    assert report["actual"]["tool_calls"] == report["new_usage"]["tool_calls"]
    assert report["actual"]["model_calls"] == report["new_usage"]["model_calls"] > 0
    with history.engine.connect() as connection:
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            ).mappings()
        )
        assert calls and all(call["origin_call_id"] is None for call in calls)
    assert history.branch(session.repository_id) == original
    assert history.restore(session.repository_id, commits[-1]).data == final.model_dump(mode="json")
    assert any(
        item["commit_id"] != source
        for item in VersionService(history).log(session.repository_id, branch_name="live-fresh")[
            "items"
        ]
    )


@pytest.mark.parametrize("crash", [False, True])
def test_live_recovery_keeps_baseline_and_supports_recorded_of_its_new_history(
    history, replay_source, crash
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    submitted = manager.replay(
        session.repository_id,
        LiveReplay(
            source_run_id=str(session.run_id),
            from_commit=commits[16],
            branch_name="live-roundtrip",
            budget=BudgetLimits(max_model_calls=2, max_steps=2),
        ),
        "live-roundtrip",
    )
    job_id = UUID(submitted["job_id"])
    crashed = False

    def fault(point):
        nonlocal crashed
        if crash and point == "after_result" and not crashed:
            crashed = True
            raise RuntimeError("Live 已保存结果后崩溃")

    if crash:
        with pytest.raises(RuntimeError, match="Live 已保存"):
            Worker(manager.jobs, fault=fault).run_once()
        before = manager.jobs.get(session.repository_id, job_id)
        with history.engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == job_id)
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    current = manager.jobs.get(session.repository_id, job_id)
    if crash:
        assert current["run_id"] != UUID(submitted["run_id"])
        assert current["deadline_at"] == before["deadline_at"]
    report = manager.replay_status(session.repository_id, job_id)["replay"]
    assert report["new_usage"]["model_calls"] == 2
    assert report["actual"]["model_calls"] == 2
    assert report["new_usage"]["tool_calls"] == 2
    chain = VersionService(history).log(session.repository_id, branch_name="live-roundtrip")[
        "items"
    ]
    first = next(
        item["commit_id"]
        for item in reversed(chain)
        if item["environment_ref"] != chain[-1]["environment_ref"]
    )
    recorded = manager.replay(
        session.repository_id,
        RecordedReplay(
            source_run_id=str(current["run_id"]),
            from_commit=first,
            until_commit=current["checkpoint_commit_id"],
            branch_name="recorded-live",
        ),
        "recorded-live",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    proof = manager.replay_status(session.repository_id, UUID(recorded["job_id"]))["replay"]
    assert proof["verified"] and proof["actual"]["model_calls"] == 0
    assert proof["actual"]["tool_calls"] == 0


def test_live_api_and_cli_share_strict_overrides_and_budget_files(
    history, replay_source, monkeypatch, tmp_path
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    monkeypatch.setattr(ManagementService, "from_settings", lambda *args, **kwargs: manager)
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {"path": "branch-note.txt", "text": "独立输入"},
                ],
            }
        ),
        encoding="utf-8",
    )
    budget = tmp_path / "budget.json"
    budget.write_bytes(encode(BudgetLimits(max_model_calls=2, max_steps=2).model_dump(mode="json")))
    token = "fixture-live-token-00000000000000000000000000"
    value = {
        "schema_version": 1,
        "mode": "live",
        "source_run_id": str(session.run_id),
        "from_commit": commits[16],
        "branch_name": "entry-live",
        "overrides": json.loads(overrides.read_text(encoding="utf-8")),
        "budget": json.loads(budget.read_bytes()),
    }
    with TestClient(
        create_app(Settings(api_token=SecretStr(token)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(session.repository_id)},
    ) as client:
        response = client.post("/replays", json=value, headers={"Idempotency-Key": "entry-live"})
        assert response.status_code == 202, response.text
        cli = CliRunner().invoke(
            app,
            [
                "replay",
                str(session.repository_id),
                "--source-run",
                str(session.run_id),
                "--from-commit",
                commits[16],
                "--branch-name",
                "entry-live",
                "--key",
                "entry-live",
                "--mode",
                "live",
                "--overrides-file",
                str(overrides),
                "--budget-file",
                str(budget),
            ],
        )
        assert cli.exit_code == 0, cli.stdout
        assert json.loads(cli.stdout) == response.json()
        assert (
            client.post(
                "/replays",
                json={**value, "until_commit": commits[-1]},
                headers={"Idempotency-Key": "invalid"},
            ).status_code
            == 422
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        saved = client.get(f"/replays/{response.json()['job_id']}")
        assert saved.status_code == 200 and saved.json()["replay"]["actual"]["model_calls"] == 2
    invalid = CliRunner().invoke(
        app,
        [
            "replay",
            str(session.repository_id),
            "--source-run",
            str(session.run_id),
            "--from-commit",
            commits[0],
            "--until-commit",
            commits[-1],
            "--branch-name",
            "bad",
            "--key",
            "bad",
            "--overrides-file",
            str(overrides),
        ],
    )
    assert invalid.exit_code != 0 and json.loads(invalid.stdout)["error"]["code"] == "validation"


@pytest.mark.parametrize("point", ["in_transaction", "after_checkpoint"])
def test_live_initial_checkpoint_is_atomic_and_can_be_recovered(history, replay_source, point):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    response = manager.replay(
        session.repository_id,
        LiveReplay(
            source_run_id=str(session.run_id),
            from_commit=commits[16],
            branch_name="initial-crash",
            budget=BudgetLimits(max_steps=2, max_model_calls=2),
        ),
        "initial-crash",
    )
    job_id = UUID(response["job_id"])
    fired = False

    def fault(current):
        nonlocal fired
        if current == point and not fired:
            fired = True
            raise RuntimeError("Live 初始化检查点崩溃")

    previous = history.fault
    if point == "in_transaction":
        history.fault = fault
    try:
        with pytest.raises(RuntimeError, match="初始化检查点崩溃"):
            Worker(manager.jobs, fault=fault).run_once()
    finally:
        history.fault = previous
    before = manager.jobs.get(session.repository_id, job_id)
    assert (before["checkpoint_commit_id"] is None) == (point == "in_transaction")
    run = manager.jobs.run(session.repository_id, before["run_id"])
    assert (run["checkpoint_commit_id"] == commits[16]) == (point == "in_transaction")
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    after = manager.jobs.get(session.repository_id, job_id)
    assert after["deadline_at"] == before["deadline_at"]
    report = manager.replay_status(session.repository_id, job_id)["replay"]
    assert report["new_usage"]["model_calls"] == report["actual"]["model_calls"] == 2


def test_live_ab_plan_and_same_named_artifact_changes_stay_on_their_own_branch(
    history, replay_source
):
    session, _, commits, _ = replay_source
    original = history.branch(session.repository_id)
    snapshot = history.restore(session.repository_id, commits[16])
    state = parse_contract(encode(snapshot.data), AgentState)
    manager = ManagementService(session.service)
    heads = []
    for label in ("A", "B"):
        plan = evolve(
            state.plan,
            nodes=(
                *state.plan.nodes,
                PlanNode(
                    key=f"branch-{label}",
                    kind="write_sources",
                    dependencies=(state.plan.nodes[-1].key,),
                ),
            ),
        )
        submitted = manager.replay(
            session.repository_id,
            LiveReplay(
                source_run_id=str(session.run_id),
                from_commit=commits[16],
                branch_name=f"plan-{label}",
                overrides=LiveOverrides(
                    plan=plan, artifacts=(ArtifactPatch(path="branch.txt", text=label),)
                ),
            ),
            f"plan-{label}",
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        current = manager.jobs.get(session.repository_id, UUID(submitted["job_id"]))
        heads.append(current["checkpoint_commit_id"])
        restored = history.restore(session.repository_id, heads[-1])
        assert restored.data["plan"] == plan.model_dump(mode="json")
        assert restored.data["plan_version"] == state.plan_version + 1
        artifact = next(item for item in restored.artifacts if item.path == "branch.txt")
        assert history.objects.get(artifact.object_ref, ObjectType.BLOB) == label.encode()
    assert heads[0] != heads[1]
    assert history.branch(session.repository_id) == original
    assert history.restore(session.repository_id, commits[16]) == snapshot


def test_live_artifact_paths_up_to_contract_limit_survive_input_ownership_and_commit(
    history, replay_source
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    paths = ("a" * 96, "nested/" + "b" * 233)
    response = manager.replay(
        session.repository_id,
        LiveReplay(
            source_run_id=str(session.run_id),
            from_commit=commits[16],
            branch_name="long-paths",
            overrides=LiveOverrides(
                artifacts=tuple(ArtifactPatch(path=path, text=str(len(path))) for path in paths)
            ),
        ),
        "long-paths",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    job = manager.jobs.get(session.repository_id, UUID(response["job_id"]))
    snapshot = history.restore(session.repository_id, job["checkpoint_commit_id"])
    artifacts = {item.path: item.object_ref for item in snapshot.artifacts}
    for path in paths:
        assert history.objects.get(artifacts[path], ObjectType.BLOB) == str(len(path)).encode()
    with history.engine.connect() as connection:
        roles = (
            connection.execute(
                sa.select(TABLES["object_refs"].c.role).where(
                    TABLES["object_refs"].c.owner_id == response["job_id"]
                )
            )
            .scalars()
            .all()
        )
    assert roles and max(map(len, roles)) <= 100
    chain = VersionService(history).log(session.repository_id, branch_name="long-paths")["items"]
    first = next(
        item["commit_id"]
        for item in reversed(chain)
        if item["environment_ref"] == chain[0]["environment_ref"]
    )
    recorded = manager.replay(
        session.repository_id,
        RecordedReplay(
            source_run_id=str(job["run_id"]),
            from_commit=first,
            until_commit=job["checkpoint_commit_id"],
            branch_name="recorded-long-paths",
        ),
        "recorded-long-paths",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    assert manager.replay_status(session.repository_id, UUID(recorded["job_id"]))["replay"][
        "verified"
    ]


def test_recorded_partial_live_range_reports_remaining_quota_against_frozen_baseline(
    history, replay_source
):
    session, _, commits, _ = replay_source
    manager = ManagementService(session.service)
    live = manager.replay(
        session.repository_id,
        LiveReplay(
            source_run_id=str(session.run_id),
            from_commit=commits[16],
            branch_name="quota-live",
            budget=BudgetLimits(max_steps=2, max_model_calls=2),
        ),
        "quota-live",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    job = manager.jobs.get(session.repository_id, UUID(live["job_id"]))
    baseline = manager.jobs.payload(job).replay.budget_baseline
    chain = VersionService(history).log(session.repository_id, branch_name="quota-live")["items"]
    selected = [
        item for item in reversed(chain) if item["environment_ref"] == chain[0]["environment_ref"]
    ]
    target = next(
        item["commit_id"]
        for item in selected
        if history.restore(session.repository_id, item["commit_id"]).data["usage"]["model_calls"]
        == baseline.model_calls + 1
    )
    replay = manager.replay(
        session.repository_id,
        RecordedReplay(
            source_run_id=live["run_id"],
            from_commit=selected[0]["commit_id"],
            until_commit=target,
            branch_name="quota-recorded",
        ),
        "quota-recorded",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    report = manager.replay_status(session.repository_id, UUID(replay["job_id"]))
    assert report["budget"]["remaining"]["steps"] == 1
    assert report["budget"]["remaining"]["model_calls"] == 1
    assert report["budget"]["baseline"] == baseline.model_dump(mode="json")
