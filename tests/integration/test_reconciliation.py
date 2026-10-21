"""S4-T05：真实写服务落地断连，可信核对后继续且不重复写入。"""

import runpy
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_approvals import effect_server as effect_server

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, ResearchGoal
from agentgit.runtime.ports import VerifiedOutcome
from agentgit.services.approvals import ApprovalService
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.reconciliation import ReconciliationService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
HELPER = Path(__file__).resolve().parents[1] / "helpers/effectful_worker.py"
EffectfulWorker = runpy.run_path(str(HELPER))["EffectfulWorker"]


@pytest.fixture
def unknown(history, effect_server, tmp_path, request):
    adapter, counts, _ = effect_server
    research = ResearchService(history, tmp_path / "reconcile-data")
    repo = history.create_repository("核对后继续")
    config = research.create_config(repo)
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    response = queue.enqueue(
        repo,
        task,
        actor="fixture-operator",
        key="effectful-run",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    run_id, job_id = UUID(response.run_id), UUID(response.job_id)
    namespace = run_id

    def worker(**kwargs):
        return EffectfulWorker(queue, adapter.base_url, namespace, **kwargs)

    assert worker().run_once()["status"] == "waiting_approval"
    with history.engine.connect() as connection:
        approval = connection.execute(sa.select(TABLES["approvals"])).mappings().one()
    policy = parse_contract(
        history.objects.get(approval["policy_digest"], ObjectType.BLOB), ToolPolicy
    )
    ApprovalService(history, policy, actor="fixture-operator").decide(
        repo,
        approval["id"],
        actor="fixture-operator",
        decision="approve",
        expected_version=queue.run(repo, run_id)["state_version"],
    )
    if getattr(request, "param", None) == "crash_before_stop":
        with history.engine.connect() as connection:
            schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
        killed = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                schema,
                str(research.data_root),
                str(history.objects.root),
                adapter.base_url,
                str(namespace),
                "after_unknown_result",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert killed.returncode == 73, killed.stderr.decode(errors="replace")
        with history.engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == job_id)
                .values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
            )
    result = worker().run_once()
    assert result["status"] == "outcome_unknown", result
    run_id = UUID(result["run_id"])
    assert counts()["requests"] == 1
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.status == "outcome_unknown"
                )
            )
            .mappings()
            .one()
        )
    return queue, repo, run_id, job_id, dict(call), worker, counts, adapter


def confirm(unknown, resolver=None, version=None, actor="fixture-operator"):
    queue, repo, run, _, call, worker, _, _ = unknown
    return ReconciliationService(queue.history).confirm(
        repo,
        run,
        call["id"],
        actor=actor,
        expected_version=version if version is not None else queue.run(repo, run)["state_version"],
        resolver=resolver or worker().resolver,
    )


def state(unknown):
    queue, repo, _, job_id, *_ = unknown
    checkpoint = queue.get(repo, job_id)["checkpoint_commit_id"]
    return parse_contract(encode(queue.history.restore(repo, checkpoint).data), AgentState)


def test_confirmed_effect_is_consumed_once_and_run_completes(unknown):
    queue, repo, run, job_id, call, worker, counts, _ = unknown
    before = queue.run(repo, run)
    old_content = queue.history.restore(repo, before["checkpoint_commit_id"]).data
    resolver = worker().resolver
    resolution = confirm(unknown, resolver)
    assert resolution["call_id"] == call["id"]
    assert resolver.lookups == 1
    assert queue.get(repo, job_id)["status"] == "queued"
    assert queue.run(repo, run)["status"] == "outcome_unknown"
    assert confirm(unknown, resolver, before["state_version"]) == resolution
    assert resolver.lookups == 1
    done = worker().run_once()
    assert done["status"] == "completed", done
    assert done["run_id"] == str(run)
    actual = state(unknown)
    assert actual.usage.tool_calls == 9
    assert actual.usage.model_calls == actual.usage.steps == 11
    assert actual.usage.reserved_tokens == 0
    assert len(actual.messages) == 20
    assert all(trace.error_code is None for trace in actual.messages)
    assert counts()["requests"] == len(counts()["entries"]) == 1
    assert queue.history.restore(repo, before["checkpoint_commit_id"]).data == old_content
    with (
        queue.history.engine.begin() as connection,
        pytest.raises(DBAPIError, match="immutable_history"),
    ):
        connection.execute(TABLES["call_resolutions"].delete())


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_confirmed_result_settles_before_cancel_or_expired_budget(unknown, stop):
    queue, repo, run, job_id, _, worker, counts, _ = unknown
    if stop == "cancel":
        queue.request_control(
            repo, run, "cancel", expected_version=queue.run(repo, run)["state_version"]
        )
    else:
        with queue.history.engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == job_id)
                .values(deadline_at=sa.func.now() - sa.text("interval '1 second'"))
            )
    confirm(unknown)
    done = worker().run_once()
    assert done["status"] == ("cancelled" if stop == "cancel" else "budget_exhausted"), done
    actual = state(unknown)
    assert actual.usage.tool_calls == 1
    assert actual.usage.model_calls == 2
    assert counts()["requests"] == 1


def test_absent_receipt_preserves_unknown_and_never_requeues(unknown):
    queue, repo, _, job_id, _, worker, counts, _ = unknown
    resolver = worker().resolver
    resolver.resolve = lambda _: None
    assert confirm(unknown, resolver) is None
    assert queue.get(repo, job_id)["status"] == "outcome_unknown"
    assert worker().run_once() is None
    assert counts()["requests"] == 1
    with queue.history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["call_resolutions"])
            ).scalar_one()
            == 0
        )


def test_query_has_no_transaction_and_stale_control_version_rejects_attachment(unknown):
    queue, repo, run, job_id, _, worker, counts, _ = unknown
    resolver = worker().resolver
    original = resolver.resolve

    def query(request):
        with queue.history.engine.begin() as connection:
            connection.execute(
                sa.select(TABLES["runs"])
                .where(TABLES["runs"].c.id == run)
                .with_for_update(nowait=True)
            )
            connection.execute(
                sa.select(TABLES["jobs"])
                .where(TABLES["jobs"].c.id == job_id)
                .with_for_update(nowait=True)
            )
        queue.request_control(
            repo, run, "cancel", expected_version=queue.run(repo, run)["state_version"]
        )
        return original(request)

    resolver.resolve = query
    with pytest.raises(AgentGitError) as caught:
        confirm(unknown, resolver)
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    assert queue.get(repo, job_id)["status"] == "outcome_unknown"
    assert counts()["requests"] == 1


def test_resolution_transaction_failure_rolls_back_result_and_job(unknown, monkeypatch):
    queue, repo, _, job_id, call, _, counts, _ = unknown
    original = queue.history._event

    def fail(*args):
        if args[-2].type == "call.reconciled":
            raise RuntimeError("注入核对事务失败")
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(queue.history, "_event", fail)
        with pytest.raises(RuntimeError, match="注入"):
            confirm(unknown)
    with queue.history.engine.connect() as connection:
        current = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.id == call["id"]
                )
            )
            .mappings()
            .one()
        )
        assert current["result_ref"] is None
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["call_resolutions"])
            ).scalar_one()
            == 0
        )
    assert queue.get(repo, job_id)["status"] == "outcome_unknown"
    assert confirm(unknown) is not None
    assert counts()["requests"] == 1


@pytest.mark.parametrize("bad", ["actor", "provider", "signature", "version", "repository", "call"])
def test_unmatched_resolution_is_rejected_before_lookup(unknown, bad):
    queue, repo, run, _, call, worker, counts, _ = unknown
    resolver = worker().resolver
    resolver.resolve = lambda _: pytest.fail("不匹配的核对不能访问服务")
    actor, version, target_repo, target_call = (
        "fixture-operator",
        queue.run(repo, run)["state_version"],
        repo,
        call["id"],
    )
    if bad == "actor":
        actor = "intruder"
    elif bad in ("provider", "signature"):
        setattr(resolver, bad, "unregistered" if bad == "provider" else "0" * 64)
    elif bad == "version":
        version -= 1
    elif bad == "repository":
        target_repo = queue.history.create_repository("其他项目")
    else:
        target_call = uuid4()
    with pytest.raises(AgentGitError):
        ReconciliationService(queue.history).confirm(
            target_repo, run, target_call, actor=actor, expected_version=version, resolver=resolver
        )
    assert counts()["requests"] == 1


@pytest.mark.parametrize("point", ["after_claim", "after_checkpoint"])
def test_process_death_after_confirmation_recovers_without_another_write(unknown, point):
    queue, repo, run, job_id, _, worker, counts, adapter = unknown
    confirm(unknown)
    with queue.history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    killed = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            schema,
            str(queue.research.data_root),
            str(queue.history.objects.root),
            adapter.base_url,
            str(run),
            point,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert killed.returncode == 73, killed.stderr.decode(errors="replace")
    with queue.history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job_id)
            .values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
        )
    done = worker().run_once()
    assert done["status"] == "completed", done
    assert done["run_id"] != str(run)
    assert queue.run(repo, run)["status"] == "interrupted"
    assert state(unknown).usage.tool_calls == 9
    assert counts()["requests"] == len(counts()["entries"]) == 1


@pytest.mark.parametrize("lost_step,cancel", [(1, False), (11, True)])
def test_model_unknown_reservation_is_settled_without_another_model_call(
    history, tmp_path, lost_step, cancel
):
    research = ResearchService(history, tmp_path / "model-reconcile")
    repo = history.create_repository("模型预留结算")
    config = research.create_config(repo)
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    response = queue.enqueue(
        repo,
        task,
        actor="model-operator",
        key="model-1",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    run, job = UUID(response.run_id), UUID(response.job_id)
    journal, actual, responses = {}, [], []

    class LostModel(MockResearchModel):
        def invoke(self, request):
            actual.append(request)
            journal[request] = super().invoke(request)
            responses.append(journal[request])
            if len(actual) == lost_step:
                raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "模型已完成但回执连接丢失")
            return journal[request]

    assert Worker(queue, model_factory=LostModel).run_once()["status"] == "outcome_unknown"
    unknown = parse_contract(
        encode(history.restore(repo, queue.get(repo, job)["checkpoint_commit_id"]).data), AgentState
    )
    assert unknown.usage.model_calls == unknown.usage.steps == lost_step
    assert unknown.usage.reserved_tokens > 0
    assert unknown.usage.tokens == sum(
        len(request) + len(payload)
        for request, payload in zip(actual[:-1], responses[:-1], strict=True)
    )
    with history.engine.connect() as connection:
        call = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.status == "outcome_unknown"
                )
            )
            .mappings()
            .one()
        )

    class ModelResolver:
        provider = "llm:mock-research/v1"
        signature = MockResearchModel.signature

        def resolve(self, request):
            return VerifiedOutcome(journal[request], journal[request])

    if cancel:
        queue.request_control(
            repo, run, "cancel", expected_version=queue.run(repo, run)["state_version"]
        )
    ReconciliationService(history).confirm(
        repo,
        run,
        call["id"],
        actor="model-operator",
        expected_version=queue.run(repo, run)["state_version"],
        resolver=ModelResolver(),
    )

    class CountModel(MockResearchModel):
        def invoke(self, request):
            actual.append(request)
            payload = super().invoke(request)
            responses.append(payload)
            return payload

    done = Worker(queue, model_factory=CountModel).run_once()
    assert done["status"] == ("cancelled" if cancel else "completed"), done
    completed = parse_contract(
        encode(history.restore(repo, queue.get(repo, job)["checkpoint_commit_id"]).data), AgentState
    )
    assert completed.usage.model_calls == completed.usage.steps == len(actual) == 11
    if cancel:
        assert queue.run(repo, run)["control_status"] == "applied"
    assert completed.usage.reserved_tokens == 0
    assert completed.usage.tokens == sum(
        len(request) + len(payload) for request, payload in zip(actual, responses, strict=True)
    )


@pytest.mark.parametrize("unknown", ["crash_before_stop"], indirect=True)
def test_original_run_call_can_be_reconciled_from_recovery_run(unknown):
    queue, repo, run, _, call, worker, counts, _ = unknown
    assert call["run_id"] != run
    assert queue.run(repo, call["run_id"])["status"] == "interrupted"
    confirm(unknown)
    done = worker().run_once()
    assert done["status"] == "completed", done
    assert state(unknown).usage.tool_calls == 9
    assert counts()["requests"] == 1


@pytest.mark.parametrize("target", ["evidence", "response", "result_link"])
def test_missing_or_mismatched_resolution_has_no_execution_fallback(unknown, target):
    queue, _, _, _, call, worker, counts, _ = unknown
    resolution = confirm(unknown)
    if target == "evidence":
        queue.history.objects.path_for(resolution["evidence_ref"]).unlink()
    elif target == "response":
        saved = queue.history.objects.get_json(resolution["result_ref"], ObjectType.CALL)
        queue.history.objects.path_for(saved["response_ref"]).unlink()
    else:
        with queue.history.engine.begin() as connection:
            other = connection.execute(
                sa.select(TABLES["external_calls"].c.result_ref).where(
                    TABLES["external_calls"].c.provider.like("llm:%")
                )
            ).scalar()
            connection.execute(
                TABLES["external_calls"]
                .update()
                .where(TABLES["external_calls"].c.id == call["id"])
                .values(result_ref=other)
            )
    done = worker().run_once()
    assert done["status"] == "failed", done
    assert done["error_code"] in (ErrorCode.NOT_FOUND.value, ErrorCode.OBJECT_CORRUPT.value)
    assert counts()["requests"] == 1


def test_incomplete_lookup_receipt_keeps_run_unknown(unknown, monkeypatch):
    queue, repo, _, job, _, worker, counts, _ = unknown
    resolver = worker().resolver

    def incomplete(*args, **kwargs):
        raise IncompleteRead(b"partial", 100)

    monkeypatch.setattr(resolver.adapter.opener, "open", incomplete)
    with pytest.raises(AgentGitError) as caught:
        confirm(unknown, resolver)
    assert caught.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert queue.get(repo, job)["status"] == "outcome_unknown"
    assert counts()["requests"] == 1


def test_concurrent_confirmation_returns_one_durable_resolution(unknown):
    queue, repo, run, _, _, _, counts, _ = unknown
    version = queue.run(repo, run)["state_version"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: confirm(unknown, version=version), range(2)))
    assert results[0] == results[1]
    with queue.history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["call_resolutions"])
            ).scalar_one()
            == 1
        )
    assert counts()["requests"] == 1


@pytest.mark.parametrize("invalid", ["oversize", "no_evidence"])
def test_unbounded_or_unproven_confirmation_is_not_published(unknown, invalid):
    queue, repo, _, job, call, worker, counts, _ = unknown
    resolver = worker().resolver
    intent = queue.history.objects.get_json(call["intent_ref"], ObjectType.CALL)
    resolver.resolve = lambda _: (
        VerifiedOutcome(b"x" * (intent["max_response_bytes"] + 1), b"proof")
        if invalid == "oversize"
        else VerifiedOutcome(b"{}", b"")
    )
    with pytest.raises(AgentGitError) as caught:
        confirm(unknown, resolver)
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT
    assert queue.get(repo, job)["status"] == "outcome_unknown"
    with queue.history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["call_resolutions"])
            ).scalar_one()
            == 0
        )
    assert counts()["requests"] == 1
