"""S4：真实 PostgreSQL 与独立 HTTP 写服务验证精确审批。"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

import pytest
import sqlalchemy as sa

from agentgit.adapters.fake_effect import FakeEffectAdapter, FakeEffectRequest
from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import encode
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass, EnvironmentManifest, RunStatus
from agentgit.core.research import ResearchGoal, evolve
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.history import TABLES, EventInput
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def effect_server(tmp_path):
    ready, journal = tmp_path / "ready.json", tmp_path / "effect-journal.json"
    helper = Path(__file__).resolve().parents[1] / "helpers/fake_effect_server.py"
    process = subprocess.Popen(
        [sys.executable, str(helper), str(ready), str(journal)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode(errors="replace"))
            assert monotonic() < deadline, "fake effect 服务未就绪"
            Event().wait(0.05)
        port = json.loads(ready.read_text(encoding="utf-8"))["port"]
        url = f"http://127.0.0.1:{port}"

        def counts():
            with build_opener(ProxyHandler({})).open(url + "/state", timeout=2) as response:
                return json.loads(response.read(8192))

        yield FakeEffectAdapter(url), counts, journal
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


@pytest.fixture
def action(history, effect_server):
    adapter, counts, journal = effect_server
    repo = history.create_repository("精确效果审批")
    run = history.create_run(repo)
    environment = EnvironmentManifest(
        runtime_version="0.1.0",
        python_version="3.12",
        code_revision="approval-fixture",
        dependency_lock_digest="a" * 64,
        model_signature="no-model",
        prompt_digest="b" * 64,
        input_ref=history.register_input(repo, b"fake-input"),
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )
    history.commit(
        repo,
        run,
        expected_head=None,
        expected_version=0,
        state={"fixture": "before-effect"},
        environment=environment,
        events=(EventInput("fixture.ready", "effect", {}),),
    )
    policy = ToolPolicy(
        rules=(
            ToolRule(
                provider=adapter.provider,
                signature_digest=adapter.signature,
                effect=EffectClass.NON_IDEMPOTENT_WRITE,
                scope="external",
            ),
        )
    )
    approvals = ApprovalService(history, policy, actor="fixture-operator")

    def checkpoint():
        branch = history.branch(repo)
        return branch["head_commit_id"], branch["version"]

    gateway = RecordedCallGateway(history, repo, run, checkpoint, authorization=approvals)
    request = encode(
        FakeEffectRequest(operation_id=str(uuid4()), value="approved-value").model_dump(mode="json")
    )
    options = dict(
        step="external-write",
        attempt=1,
        provider=adapter.provider,
        signature=adapter.signature,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request=request,
        invoke=lambda: adapter.invoke(request),
        max_bytes=4096,
    )
    return history, repo, run, approvals, gateway, options, adapter, counts, journal, environment


def pending(action):
    history, repo, run, approvals, gateway, options, _, counts, _, _ = action
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.APPROVAL_REQUIRED
    assert counts()["requests"] == 0
    row = approvals.list_for_run(repo, run)[-1]
    with history.engine.connect() as connection:
        current = history._run(connection, repo, run)
    assert current["status"] == "waiting_approval"
    assert history.branch(repo)["active_writer_run_id"] == run
    return row, current["state_version"]


def decide(action, choice="approve"):
    row, version = pending(action)
    _, repo, _, approvals, *_ = action
    result = approvals.decide(
        repo, row["id"], actor="fixture-operator", decision=choice, expected_version=version
    )
    return result, version


def test_approval_before_after_and_duplicate_requests_execute_once(action):
    _, repo, run, approvals, gateway, options, _, counts, _, _ = action
    approved, version = decide(action)
    assert (
        approvals.decide(
            repo,
            approved["id"],
            actor="fixture-operator",
            decision="approve",
            expected_version=version,
        )
        == approved
    )
    with pytest.raises(AgentGitError) as caught:
        approvals.decide(
            repo,
            approved["id"],
            actor="fixture-operator",
            decision="reject",
            expected_version=version,
        )
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    first = gateway.call(**options)
    assert first.error is None
    assert gateway.call(**options) == first
    state = counts()
    assert state["requests"] == len(state["entries"]) == 1
    consumed = approvals.list_for_run(repo, run)[0]
    assert consumed["status"] == "consumed"
    assert consumed["consumed_at"] is not None


def test_rejection_never_sends_effect(action):
    _, _, _, _, gateway, options, _, counts, _, _ = action
    decide(action, "reject")
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert counts()["requests"] == 0


@pytest.mark.parametrize("after_approval", [False, True])
def test_expiration_never_sends_effect(action, after_approval):
    history, repo, _, approvals, gateway, options, _, counts, _, _ = action
    row, version = decide(action) if after_approval else pending(action)
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["approvals"]
            .update()
            .where(TABLES["approvals"].c.id == row["id"])
            .values(expires_at=sa.func.now() - sa.text("interval '1 second'"))
        )
    if not after_approval:
        with pytest.raises(AgentGitError) as caught:
            approvals.decide(
                repo,
                row["id"],
                actor="fixture-operator",
                decision="approve",
                expected_version=version,
            )
        assert caught.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(AgentGitError):
        gateway.call(**options)
    assert counts()["requests"] == 0


@pytest.mark.parametrize("change", ["arguments", "checkpoint", "policy", "response_limit"])
def test_changed_binding_cannot_consume_original_approval(action, change):
    history, repo, run, approvals, gateway, options, adapter, counts, _, environment = action
    original, _ = decide(action)
    if change == "arguments":
        request = encode(
            FakeEffectRequest(operation_id=str(uuid4()), value="changed").model_dump(mode="json")
        )
        options = {**options, "request": request, "invoke": lambda: adapter.invoke(request)}
    elif change == "checkpoint":
        branch = history.branch(repo)
        history.commit(
            repo,
            run,
            expected_head=branch["head_commit_id"],
            expected_version=branch["version"],
            state={"fixture": "changed"},
            environment=environment,
            events=(EventInput("fixture.changed", "effect", {}),),
        )
    elif change == "policy":
        changed = ToolPolicy(version=2, rules=approvals.policy.rules)
        gateway.authorization = ApprovalService(history, changed, actor="fixture-operator")
    else:
        options = {**options, "max_bytes": 4095}
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.APPROVAL_REQUIRED
    assert counts()["requests"] == 0
    assert approvals.list_for_run(repo, run)[0]["id"] == original["id"]
    assert approvals.list_for_run(repo, run)[0]["status"] == "approved"
    assert len(approvals.list_for_run(repo, run)) == 2


@pytest.mark.parametrize("change", ["signature", "effect", "provider", "missing_policy"])
def test_local_registry_cannot_be_overridden_by_caller(action, change):
    _, _, _, _, gateway, options, _, counts, _, _ = action
    if change == "missing_policy":
        gateway.authorization = None
    elif change == "signature":
        options = {**options, "signature": "0" * 64}
    elif change == "effect":
        options = {**options, "effect": EffectClass.READ_ONLY}
    else:
        options = {**options, "provider": "tool:unregistered/v1"}
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert counts()["requests"] == 0


def test_approval_consume_and_intent_are_atomic(action, monkeypatch):
    history, repo, run, approvals, gateway, options, _, counts, _, _ = action
    decide(action)
    original = history._event

    def fail_started(connection, repository, run_id, seq, event, payload):
        if event.type == "tool.started":
            raise RuntimeError("注入意图事务失败")
        return original(connection, repository, run_id, seq, event, payload)

    with monkeypatch.context() as patch:
        patch.setattr(history, "_event", fail_started)
        with pytest.raises(RuntimeError, match="注入"):
            gateway.call(**options)
    assert approvals.list_for_run(repo, run)[0]["status"] == "approved"
    assert counts()["requests"] == 0
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["external_calls"])
            ).scalar_one()
            == 0
        )
    assert gateway.call(**options).error is None
    assert counts()["requests"] == 1


def test_concurrent_consumers_do_not_send_two_effects(action):
    _, _, _, _, gateway, options, _, counts, _, _ = action
    decide(action)

    def call():
        try:
            return gateway.call(**options)
        except AgentGitError as error:
            assert error.code == ErrorCode.OUTCOME_UNKNOWN
            return None

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: call(), range(2)))
    assert gateway.call(**options).error is None
    assert counts()["requests"] == 1


def test_foreign_actor_and_repository_cannot_decide(action):
    history, repo, run, approvals, _, _, _, counts, _, _ = action
    row, version = pending(action)
    other = history.create_repository("其他项目")
    for repository, actor, code in (
        (repo, "intruder", ErrorCode.FORBIDDEN),
        (other, "fixture-operator", ErrorCode.NOT_FOUND),
    ):
        with pytest.raises(AgentGitError) as caught:
            approvals.decide(
                repository, row["id"], actor=actor, decision="approve", expected_version=version
            )
        assert caught.value.code == code
    assert approvals.list_for_run(repo, run)[0]["status"] == "pending"
    assert counts()["requests"] == 0


def test_applied_then_disconnected_is_unknown_and_never_resent(action):
    history, repo, run, approvals, gateway, options, adapter, counts, journal, _ = action
    request = encode(
        FakeEffectRequest(
            operation_id=str(uuid4()), value="durable-before-drop", response_mode="drop"
        ).model_dump(mode="json")
    )
    options = {**options, "request": request, "invoke": lambda: adapter.invoke(request)}
    modified = (*action[:5], options, *action[6:])
    decide(modified)
    outcome = gateway.call(**options)
    assert outcome.error.code == ErrorCode.OUTCOME_UNKNOWN
    assert len(json.loads(journal.read_bytes())["entries"]) == 1
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert counts()["requests"] == 1
    receipt = adapter.lookup(request)
    assert receipt is not None
    assert json.loads(receipt)["value"] == "durable-before-drop"
    with history.engine.connect() as connection:
        call = connection.execute(sa.select(TABLES["external_calls"])).mappings().one()
    assert call["status"] == "outcome_unknown"
    assert call["result_ref"] is None
    assert approvals.list_for_run(repo, run)[0]["status"] == "consumed"


@pytest.mark.parametrize("decision", ["approve", "expire", "cancel"])
def test_managed_job_parks_and_reclaims_with_new_lease(history, effect_server, tmp_path, decision):
    adapter, counts, _ = effect_server
    research = ResearchService(history, tmp_path / "managed-approval")
    repo = history.create_repository("受调度的审批")
    config = research.create_config(repo)
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    queue.enqueue(
        repo,
        task,
        actor="fixture-operator",
        key="managed-1",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    lease = queue.claim("first")
    session, _, _ = Worker(queue)._attach(lease)
    session.environment = session._environment(MockResearchModel())
    session.save(evolve(session.state, status=RunStatus.PLANNING), "run.planning", {})
    checkpoint = session.head
    policy = ToolPolicy(
        rules=(
            ToolRule(
                provider=adapter.provider,
                signature_digest=adapter.signature,
                effect=EffectClass.NON_IDEMPOTENT_WRITE,
                scope="external",
            ),
        )
    )
    authorization = ApprovalService(session.history, policy, actor="fixture-operator")
    gateway = RecordedCallGateway(
        session.history,
        repo,
        session.run_id,
        lambda: (session.head, session.version),
        authorization=authorization,
    )
    request = encode(
        FakeEffectRequest(operation_id=str(uuid4()), value="managed").model_dump(mode="json")
    )
    options = dict(
        step="managed-write",
        attempt=1,
        provider=adapter.provider,
        signature=adapter.signature,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request=request,
        invoke=lambda: adapter.invoke(request),
        max_bytes=4096,
    )
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.APPROVAL_REQUIRED
    job = queue.get(repo, lease.job_id)
    assert job["status"] == "waiting_approval"
    assert job["worker"] is job["lease_until"] is None
    assert job["checkpoint_commit_id"] == checkpoint
    assert queue.claim("unapproved") is None
    approval = authorization.list_for_run(repo, session.run_id)[0]
    current = queue.run(repo, session.run_id)
    if decision == "approve":
        authorization.decide(
            repo,
            approval["id"],
            actor="fixture-operator",
            decision="approve",
            expected_version=current["state_version"],
        )
    elif decision == "expire":
        with history.engine.begin() as connection:
            connection.execute(
                TABLES["approvals"]
                .update()
                .where(TABLES["approvals"].c.id == approval["id"])
                .values(expires_at=sa.func.now() - sa.text("interval '1 second'"))
            )
        assert ApprovalService.expire_pending(history) == 1
        assert ApprovalService.expire_pending(history) == 0
    else:
        queue.request_control(
            repo, session.run_id, "cancel", expected_version=current["state_version"]
        )
        assert Worker(queue).run_once()["status"] == "cancelled"
        assert counts()["requests"] == 0
        return
    second = queue.claim("second")
    assert second.run_id == lease.run_id
    assert second.fencing_token == lease.fencing_token + 1
    assert not second.recovery_required
    with pytest.raises(AgentGitError) as caught:
        gateway.call(**options)
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    restarted, _, _ = Worker(queue)._attach(second)
    fresh_gateway = RecordedCallGateway(
        restarted.history,
        repo,
        second.run_id,
        lambda: (restarted.head, restarted.version),
        authorization=ApprovalService(restarted.history, policy, actor="fixture-operator"),
    )
    if decision == "approve":
        assert fresh_gateway.call(**options).error is None
        assert counts()["requests"] == 1
    else:
        with pytest.raises(AgentGitError) as caught:
            fresh_gateway.call(**options)
        assert caught.value.code == ErrorCode.FORBIDDEN
        assert counts()["requests"] == 0
