"""真实 PostgreSQL 的冻结搜索、原子预算意图、租约恢复及取消边界。"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_debug_candidate_experiments import candidate_case as candidate_case
from test_recorded_replayer import replay_source as replay_source

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.config import Settings, database_url
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.interventions import PromptPatch
from agentgit.core.repair import PhaseQuotas, RepairBinding, RepairBudget, RepairLease
from agentgit.core.research import AgentConfig, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.space import freeze_space
from agentgit.repair.templates import TemplateAnchor, model_templates
from agentgit.services.history import HistoryService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


@pytest.fixture
def repair_case(history, candidate_case):
    session, manager, report_id, event_id, bound, request = candidate_case
    context = bound["context"]
    view = manager.checkout(session.repository_id, context["base_commit"])
    anchor = TemplateAnchor.model_validate_json(
        encode(
            {
                key: context[key]
                for key in (
                    "source_run_id",
                    "base_commit",
                    "target_commit_id",
                    "target_event_id",
                    "precondition",
                )
            }
        )
    )
    generated = model_templates(
        anchor,
        AgentConfig.model_validate_json(encode(context["config"])),
        StateSnapshot.model_validate_json(encode(view["snapshot"])),
        history.objects.get,
    )
    binding = RepairBinding(
        repository_id=str(session.repository_id),
        failed_run_id=context["source_run_id"],
        base_commit=context["base_commit"],
        environment_ref=view["manifest"]["environment_ref"],
        validator_ref=context["precondition"]["validator_ref"],
    )
    space = freeze_space(binding, generated.patches, source_reason="定位未达标，完整候选列表降级")
    identities = tuple(
        manager.create_intervention(
            session.repository_id,
            patch.intervention,
            f"ledger-template-{index}",
        )["intervention_id"]
        for index, patch in enumerate(generated.patches)
    )
    return session.repository_id, space, identities


def submit(history, repair_case, *, key="ledger", budget=None):
    repo, space, identities = repair_case
    result = RepairSearchService(history).submit(
        space, budget or RepairBudget(), identities, actor="repair-operator", key=key
    )
    return repo, UUID(result["search_id"]), RepairJobService(history), result


def table_counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(TABLES[name])
            ).scalar_one()
            for name in (
                "repair_searches",
                "repair_proposals",
                "repair_trials",
                "repair_events",
                "repair_trial_jobs",
                "repair_trial_results",
                "jobs",
                "object_refs",
            )
        }


def test_freezes_seven_proposals_and_reuses_original_request_without_execution(
    history,
    repair_case,
    monkeypatch,
):
    before = table_counts(history)

    def forbidden(*args, **kwargs):
        pytest.fail("搜索冻结或查询不能执行模型、工具或重建已缓存请求")

    monkeypatch.setattr(MockResearchModel, "invoke", forbidden)
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    repo, search, ledger, created = submit(history, repair_case)
    after = table_counts(history)
    assert after["jobs"] == before["jobs"]
    assert after["repair_searches"] == before["repair_searches"] + 1
    assert after["repair_proposals"] == before["repair_proposals"] + 7
    assert created["proposal_count"] == 7
    monkeypatch.setattr(RepairSearchService, "compile", forbidden)
    assert submit(history, repair_case)[3] == created
    assert table_counts(history) == after
    monkeypatch.setattr(history.objects, "put", forbidden)
    view = ledger.view(repo, search)
    assert view["manifest"]["baseline"]["verdict"] == "failed"
    assert view["manifest"]["space"]["candidate_source"] == "full_candidates_fallback"
    assert view["status"] == "queued" and view["trials"] == []
    assert "lease_token" not in view
    with pytest.raises(AgentGitError) as error:
        ledger.view(uuid4(), search)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_single_connection_pool_and_six_concurrent_submissions_publish_once(history, repair_case):
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    single_engine = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        single_history = HistoryService(single_engine, history.objects)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: submit(single_history, repair_case)[3], range(6)))
        assert all(result == results[0] for result in results)
        repo, search, ledger, _ = submit(single_history, repair_case)
        assert len(ledger.view(repo, search)["proposals"]) == 7
        assert table_counts(history)["repair_searches"] == 1
    finally:
        single_engine.dispose()


def test_search_publication_failure_rolls_back_and_same_key_can_retry(
    history,
    repair_case,
    monkeypatch,
):
    before = table_counts(history)
    original = history._ref

    def fail_proposal(connection, repository, obj, owner_type, owner_id, role):
        if owner_type == "repair_proposal":
            raise RuntimeError("模拟提案发布前进程失败")
        return original(connection, repository, obj, owner_type, owner_id, role)

    with monkeypatch.context() as fault:
        fault.setattr(history, "_ref", fail_proposal)
        with pytest.raises(RuntimeError):
            submit(history, repair_case)
    assert table_counts(history) == before
    assert submit(history, repair_case)[3]["proposal_count"] == 7


def test_atomic_claim_restart_and_intent_reuse_do_not_double_reserve(history, repair_case):
    repo, search, ledger, _ = submit(history, repair_case)
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(
            pool.map(lambda index: RepairJobService(history).claim(f"worker-{index}"), range(6))
        )
    active = [claim for claim in claims if claim is not None]
    assert len(active) == 1
    lease = active[0]
    first = ledger.reserve(lease)
    assert first["admitted"] and not first["reused"]
    assert ledger.reserve(lease)["trial_id"] == first["trial_id"]
    view = ledger.view(repo, search)
    assert view["usage"]["active"] == 1 and view["usage"]["started"]["screen"] == 1
    with pytest.raises(AgentGitError) as error:
        ledger.reserve(lease, UUID(view["proposals"][0]["proposal_id"]))
    assert error.value.code == ErrorCode.VALIDATION
    ledger.release(lease)
    restarted = RepairJobService(history)
    next_lease = restarted.claim("restarted-worker")
    assert next_lease.token != lease.token
    reused = restarted.reserve(next_lease)
    assert reused["reused"] and reused["trial_id"] == first["trial_id"]
    assert table_counts(history)["repair_trials"] == 1
    with pytest.raises(AgentGitError) as error:
        ledger.reserve(lease)
    assert error.value.code == ErrorCode.VERSION_CONFLICT


def test_expired_lease_is_fenced_and_new_owner_keeps_existing_intent(history, repair_case):
    repo, search, ledger, _ = submit(history, repair_case)
    first = ledger.claim("first")
    intent = ledger.reserve(first)
    ledger.release(first)
    short = RepairJobService(history, lease_ms=100)
    expired = short.claim("short-owner")
    time.sleep(0.2)
    recovered = ledger.claim("new-owner")
    assert recovered is not None and recovered.token != expired.token
    assert ledger.reserve(recovered)["trial_id"] == intent["trial_id"]
    with pytest.raises(AgentGitError) as error:
        short.heartbeat(expired)
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    assert ledger.view(repo, search)["usage"]["active"] == 1


def test_cancel_retains_full_reservation_until_proven_undispatched(history, repair_case):
    repo, search, ledger, _ = submit(history, repair_case)
    lease = ledger.claim("worker")
    intent = ledger.reserve(lease)
    assert ledger.cancel(repo, search, actor="operator")["status"] == "cancelling"
    assert ledger.view(repo, search)["usage"]["active"] == 1
    counts = table_counts(history)
    assert ledger.cancel(repo, search, actor="operator")["status"] == "cancelling"
    assert table_counts(history) == counts
    with pytest.raises(AgentGitError) as error:
        ledger.reserve(lease)
    assert error.value.code == ErrorCode.CONTROL_REQUESTED
    assert ledger.abandon_undispatched(lease) == 1
    view = ledger.view(repo, search)
    assert view["status"] == "cancelled"
    assert view["usage"]["active"] == 0
    assert view["usage"]["started"]["screen"] == 1
    assert view["trials"][0]["trial_id"] == intent["trial_id"]
    assert ledger.claim("later") is None


def test_reservation_audit_failure_rolls_back_budget_and_intent(history, repair_case, monkeypatch):
    repo, search, ledger, _ = submit(history, repair_case)
    lease = ledger.claim("worker")
    before = table_counts(history)

    def fail(*args, **kwargs):
        raise RuntimeError("模拟意图写入后的审计失败")

    with monkeypatch.context() as fault:
        fault.setattr(ledger, "_update", fail)
        with pytest.raises(RuntimeError):
            ledger.reserve(lease)
    assert table_counts(history) == before
    assert ledger.view(repo, search)["usage"]["started"]["screen"] == 0
    assert ledger.reserve(lease)["intent"]["ordinal"] == 0


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"max_tokens": 43999}, "tokens"),
        ({"max_model_calls": 65}, "model_calls"),
        ({"max_tool_calls": 131}, "tool_calls"),
        ({"max_wall_ms": 659999}, "wall_time"),
        ({"quotas": PhaseQuotas(screen=0)}, "phase_trials"),
    ],
)
def test_atomic_budget_denial_creates_no_trial_and_preserves_evidence(
    history,
    repair_case,
    changes,
    reason,
):
    repo, search, ledger, _ = submit(history, repair_case, budget=evolve(RepairBudget(), **changes))
    lease = ledger.claim("worker")
    result = ledger.reserve(lease)
    assert not result["admitted"] and result["decision"]["reason"] == reason
    view = ledger.view(repo, search)
    assert view["status"] == "budget_exhausted" and view["trials"] == []
    assert view["manifest"]["baseline"]["verdict"] == "failed"
    assert ledger.claim("retry") is None


def test_different_key_payload_or_unpublished_patch_is_rejected(history, repair_case):
    repo, space, identities = repair_case
    service = RepairSearchService(history)
    submit(history, repair_case)
    with pytest.raises(AgentGitError) as error:
        service.submit(
            space,
            evolve(RepairBudget(), max_tokens=59999),
            identities,
            actor="repair-operator",
            key="ledger",
        )
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    altered = evolve(
        space.patches[0],
        intervention=evolve(
            space.patches[0].intervention, payload=PromptPatch(prompt="假冒已发布效果")
        ),
    )
    bad_space = freeze_space(space.binding, (altered,), source_reason="测试内容不匹配")
    before = table_counts(history)
    with pytest.raises(AgentGitError) as error:
        service.submit(bad_space, RepairBudget(), (identities[0],), actor="operator", key="bad")
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
    assert table_counts(history) == before


def test_database_rejects_history_rewrite_stale_revision_and_forged_success(history, repair_case):
    repo, search, ledger, _ = submit(history, repair_case)
    lease = ledger.claim("worker")
    intent = ledger.reserve(lease)
    with history.engine.begin() as connection:
        for operation in (
            TABLES["repair_searches"].update().values(actor="rewritten"),
            TABLES["repair_proposals"].delete(),
            TABLES["repair_trials"].update().values(phase="confirm"),
            TABLES["repair_jobs"].update().values(revision=0),
            TABLES["repair_trial_results"]
            .insert()
            .values(
                id=UUID(intent["trial_id"]),
                repository_id=repo,
                verdict="passed",
                result_ref=intent["intent_ref"],
                model_calls=0,
                tool_calls=0,
                tokens=0,
                cost_micro_usd=0,
            ),
        ):
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(operation)


def test_incompatible_code_stops_claim_and_keeps_search_immutable(
    history, repair_case, monkeypatch
):
    repo, search, ledger, _ = submit(history, repair_case)
    original = ledger.view(repo, search)["manifest"]
    monkeypatch.setattr(
        "agentgit.services.repair_jobs.source_revision", lambda: "source-sha256:" + "0" * 64
    )
    assert ledger.claim("new-code-worker") is None
    view = ledger.view(repo, search)
    assert view["status"] == "needs_attention" and view["stop_reason"] == "incompatible_code"
    assert view["manifest"] == original and view["trials"] == []


def test_abrupt_independent_process_exit_keeps_committed_intent_and_fences_old_owner(
    history,
    repair_case,
):
    repo, search, ledger, _ = submit(history, repair_case)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    isolated_url = database_url(Settings.from_env().test_database_url).update_query_dict(
        {"options": f"-csearch_path={schema}"},
    )
    environment = {
        **os.environ,
        "AGENTGIT_TEST_DATABASE_URL": isolated_url.render_as_string(hide_password=False),
        "AGENTGIT_TEST_DATA_ROOT": str(history.objects.root.parent),
    }
    before = table_counts(history)
    child = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("repair_ledger_process.py")), str(repo)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 73, child.stderr
    saved = json.loads(child.stdout)
    assert saved["intent"]["admitted"] and not saved["intent"]["reused"]
    time.sleep(0.6)
    recovered = RepairJobService(history).claim("replacement-process")
    assert recovered is not None
    assert recovered.token != saved["lease"]["token"]
    current = ledger.reserve(recovered)
    assert current["reused"] and current["trial_id"] == saved["intent"]["trial_id"]
    after = table_counts(history)
    assert after["repair_trials"] == before["repair_trials"] + 1
    assert after["jobs"] == before["jobs"]
    expired = RepairLease.model_validate_json(encode(saved["lease"]))
    with pytest.raises(AgentGitError) as error:
        ledger.reserve(expired)
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    assert ledger.view(repo, search)["usage"]["active"] == 1


def test_incompatible_search_allows_cancellation_without_resuming_or_releasing_unknown_jobs(
    history,
    repair_case,
    monkeypatch,
):
    repo, search, ledger, _ = submit(history, repair_case)
    lease = ledger.claim("old-code-worker")
    original = ledger.reserve(lease)
    ledger.release(lease)
    monkeypatch.setattr(
        "agentgit.services.repair_jobs.source_revision", lambda: "source-sha256:" + "0" * 64
    )
    assert ledger.claim("new-code-worker") is None
    assert ledger.view(repo, search)["status"] == "needs_attention"
    assert ledger.cancel(repo, search, actor="operator")["status"] == "cancelling"
    canceller = ledger.claim("cancellation-only")
    assert canceller is not None
    with pytest.raises(AgentGitError) as error:
        ledger.reserve(canceller)
    assert error.value.code == ErrorCode.CONTROL_REQUESTED
    assert ledger.view(repo, search)["usage"]["active"] == 1
    assert ledger.abandon_undispatched(canceller) == 1
    view = ledger.view(repo, search)
    assert view["status"] == "cancelled" and view["usage"]["active"] == 0
    assert view["trials"][0]["trial_id"] == original["trial_id"]
