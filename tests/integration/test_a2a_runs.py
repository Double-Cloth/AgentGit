"""真实 Job／Worker／审批／CAS 的 A2A Live，不直接改写运行结果。"""

import copy
import os
import time
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from alembic import command
from alembic.config import Config
from test_a2a_fixture import CASES
from test_a2a_fixture import server as server
from test_a2a_transport import setup

from agentgit.core.a2a_runs import A2AProgram
from agentgit.core.canonical import encode
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.services.a2a_runs import A2ARunService
from agentgit.services.approvals import ApprovalService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ACTOR = "a2a-operator"


def case(history, server, tmp_path, *, messages=1, max_polls=16, interval=100, wall_ms=60000):
    _, binding, mode, counts, observed = setup(server)
    bodies = []
    for i in range(messages):
        body = copy.deepcopy(CASES["request"])
        body["message"]["messageId"] = f"declared-message-{i + 1}"
        bodies.append(encode(body).decode())
    program = A2AProgram(
        binding=binding, messages=tuple(bodies), max_polls=max_polls, poll_interval_ms=interval
    )
    if wall_ms != 60000:
        body = program.model_dump(mode="json")
        body["limits"]["max_wall_ms"] = wall_ms
        program = A2AProgram.model_validate_json(encode(body))
    queue = JobService(ResearchService(history, tmp_path / "data"))
    service = A2ARunService(queue)
    repo = history.create_repository("A2A 耐久任务")
    receipt = service.submit(
        repo,
        program,
        actor=ACTOR,
        key="live",
        branch_name="main",
        expected_head=None,
        expected_version=0,
    )
    return queue, service, repo, program, receipt, mode, counts, observed


def state(values):
    queue, service, repo, _, receipt, *_ = values
    return service.state(repo, UUID(receipt.run_id), actor=ACTOR)


def control(values, action):
    queue, _, repo, _, receipt, *_ = values
    run = UUID(receipt.run_id)
    return queue.request_control(
        repo, run, action, expected_version=queue.run(repo, run)["state_version"]
    )


def approve(values):
    queue, _, repo, program, receipt, *_ = values
    policy = ToolPolicy(
        rules=(
            program.binding.rule("send"),
            program.binding.rule("get"),
            program.binding.rule("cancel"),
        )
    )
    approvals = ApprovalService(queue.history, policy, actor=ACTOR)
    pending = approvals.list_for_run(repo, UUID(receipt.run_id))[-1]
    return approvals.decide(
        repo,
        pending["id"],
        actor=ACTOR,
        decision="approve",
        expected_version=queue.run(repo, UUID(receipt.run_id))["state_version"],
    )


def ready_step(values, **worker_kwargs):
    deadline = time.perf_counter() + 10
    while True:
        result = Worker(values[0], **worker_kwargs).run_once()
        if result is not None:
            return result
        assert time.perf_counter() < deadline, "A2A Job 未按已保存计划可领取"
        time.sleep(0.01)


def start(values):
    assert ready_step(values)["status"] == "waiting_approval"
    assert values[6]()["accepted_messages"] == 0
    approve(values)
    return ready_step(values)


def test_submit_is_idempotent_and_changed_input_conflicts(history, server, tmp_path):
    values = case(history, server, tmp_path)
    _, service, repo, program, receipt, *_ = values
    assert (
        service.submit(
            repo,
            program,
            actor=ACTOR,
            key="live",
            branch_name="main",
            expected_head=None,
            expected_version=0,
        )
        == receipt
    )
    changed = program.model_dump(mode="json")
    body = copy.deepcopy(CASES["request"])
    body["message"]["messageId"] = "different"
    changed["messages"] = [encode(body).decode()]
    with pytest.raises(AgentGitError) as error:
        service.submit(
            repo,
            A2AProgram.model_validate_json(encode(changed)),
            actor=ACTOR,
            key="live",
            branch_name="main",
            expected_head=None,
            expected_version=0,
        )
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    assert service.state(repo, UUID(receipt.run_id), actor=ACTOR) is None


@pytest.mark.parametrize("mode", ["success", "structured"])
def test_worker_requires_approval_then_publishes_safe_artifacts(history, server, tmp_path, mode):
    values = case(history, server, tmp_path)
    values[5](mode)
    assert start(values)["status"] == "completed"
    current = state(values)
    assert current.status.value == "completed" and current.usage.tool_calls == 1
    assert current.remote_state == "TASK_STATE_COMPLETED" and current.pending is None
    run = values[0].run(values[2], UUID(values[4].run_id))
    restored = history.restore(values[2], run["checkpoint_commit_id"])
    assert {entry.path for entry in restored.artifacts} == {"a2a/task.json", "a2a/artifact-1.json"}
    assert values[6]()["accepted_messages"] == 1
    assert Worker(values[0]).run_once() is None


def test_polling_releases_job_and_keeps_original_deadline(history, server, tmp_path):
    values = case(history, server, tmp_path, interval=1000)
    values[5]("working")
    assert start(values)["status"] == "queued"
    queue = values[0]
    job = queue.get(values[2], UUID(values[4].job_id))
    original = job["deadline_at"]
    assert job["worker"] is None and job["lease_until"] is None and state(values).defer_pending
    assert state(values).usage.tool_calls == 1
    values[5]("success")
    assert ready_step(values)["status"] == "completed"
    assert queue.get(values[2], UUID(values[4].job_id))["deadline_at"] == original
    assert (
        state(values).polls == 1
        and state(values).usage.tool_calls == 2
        and values[6]()["accepted_messages"] == 1
    )


def test_poll_limit_does_not_reset_or_resend_task(history, server, tmp_path):
    values = case(history, server, tmp_path, max_polls=2, interval=20)
    values[5]("working")
    start(values)
    assert ready_step(values)["status"] == "queued"
    assert ready_step(values)["status"] == "queued"
    assert ready_step(values)["status"] == "budget_exhausted"
    assert state(values).polls == 2 and state(values).usage.tool_calls == 3
    assert values[6]()["accepted_messages"] == 1


@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_queued_control_never_constructs_client(history, server, tmp_path, action, expected):
    values = case(history, server, tmp_path)
    control(values, action)
    assert (
        ready_step(values, a2a_client_factory=lambda _: pytest.fail("排队控制不能构造客户端"))[
            "status"
        ]
        == expected
    )
    assert state(values).usage.tool_calls == 0 and values[6]()["accepted_messages"] == 0


def test_pause_resume_preserves_budget_and_deadline(history, server, tmp_path):
    values = case(history, server, tmp_path)
    control(values, "pause")
    ready_step(values)
    original = values[0].get(values[2], UUID(values[4].job_id))["deadline_at"]
    control(values, "resume")
    assert start(values)["status"] == "completed"
    assert (
        state(values).usage.tool_calls == 1
        and values[0].get(values[2], UUID(values[4].job_id))["deadline_at"] == original
    )


def test_saved_result_is_consumed_before_pause_and_resume_needs_no_client(
    history, server, tmp_path
):
    values = case(history, server, tmp_path)
    assert ready_step(values)["status"] == "waiting_approval"
    approve(values)

    def fault(point):
        if point == "a2a.after_result":
            control(values, "pause")

    assert ready_step(values, fault=fault)["status"] == "paused"
    assert state(values).last_response_ref is not None and values[6]()["accepted_messages"] == 1
    control(values, "resume")
    assert (
        ready_step(values, a2a_client_factory=lambda _: pytest.fail("已保存结果不能创建客户端"))[
            "status"
        ]
        == "completed"
    )
    assert state(values).usage.tool_calls == 1 and values[6]()["accepted_messages"] == 1


def test_cancel_waiting_send_retires_approval_without_network(history, server, tmp_path):
    values = case(history, server, tmp_path)
    assert ready_step(values)["status"] == "waiting_approval"
    control(values, "cancel")
    assert (
        ready_step(
            values, a2a_client_factory=lambda _: pytest.fail("未执行发送取消不能创建客户端")
        )["status"]
        == "cancelled"
    )
    assert state(values).pending is None and len(state(values).retired) == 1
    assert values[6]()["accepted_messages"] == 0
    with pytest.raises(AgentGitError):
        approve(values)


@pytest.mark.parametrize(
    "mode,expected,remote",
    [
        ("working", "cancelled", "confirmed"),
        ("cancel_rejected", "cancelled", "rejected"),
        ("cancel_ack_working", "cancelled", "not_confirmed"),
        ("disconnect_after_cancel", "outcome_unknown", "outcome_unknown"),
    ],
)
def test_remote_cancel_requires_its_own_exact_approval(
    history, server, tmp_path, mode, expected, remote
):
    values = case(history, server, tmp_path, interval=20)
    values[5]("working")
    start(values)
    control(values, "cancel")
    values[5](mode)
    assert ready_step(values)["status"] == "waiting_approval"
    assert values[6]()["tasks"]["task-1"]["value"]["status"]["state"] == "TASK_STATE_WORKING"
    approve(values)
    assert ready_step(values)["status"] == expected
    assert state(values).remote_cancel_status == remote
    assert values[6]()["accepted_messages"] == 1
    before = len(values[6]()["requests"])
    assert Worker(values[0]).run_once() is None and len(values[6]()["requests"]) == before


def test_declared_followup_is_a_separate_approved_message_on_same_task(history, server, tmp_path):
    values = case(history, server, tmp_path, messages=2)
    values[5]("input_required")
    assert start(values)["status"] == "queued"
    values[5]("success")
    assert (
        ready_step(values)["status"] == "waiting_approval" and values[6]()["accepted_messages"] == 1
    )
    approve(values)
    assert ready_step(values)["status"] == "completed"
    assert state(values).next_message == 2 and state(values).usage.tool_calls == 2
    assert len(values[6]()["tasks"]) == 1 and values[6]()["accepted_messages"] == 2


@pytest.mark.parametrize(
    "mode,status,waiting",
    [
        ("input_required", "paused", "input"),
        ("auth_required", "paused", "auth"),
        ("remote_failed", "failed", None),
        ("remote_rejected", "failed", None),
    ],
)
def test_remote_interrupted_and_failed_states_are_not_completed(
    history, server, tmp_path, mode, status, waiting
):
    values = case(history, server, tmp_path)
    values[5](mode)
    assert start(values)["status"] == status
    assert state(values).waiting_for == waiting and state(values).status.value == status


def test_wrong_actor_repository_or_foreign_receipt_cannot_be_consumed(history, server, tmp_path):
    values = case(history, server, tmp_path)
    assert start(values)["status"] == "completed"
    queue, service, repo, program, receipt, *_ = values
    with pytest.raises(AgentGitError) as wrong_actor:
        service.state(repo, UUID(receipt.run_id), actor="other")
    assert wrong_actor.value.code == ErrorCode.FORBIDDEN
    other = history.create_repository("其他项目")
    with pytest.raises(AgentGitError):
        service.state(other, UUID(receipt.run_id), actor=ACTOR)
    foreign = history.create_run(other)
    with pytest.raises(AgentGitError) as wrong_ref:
        service.confirmed_result(other, foreign, state(values).last_response_ref, program)
    assert wrong_ref.value.code == ErrorCode.OBJECT_CORRUPT
    assert queue.run(repo, UUID(receipt.run_id))["status"] == "completed"


def test_expired_original_deadline_stops_resumed_job_before_client(history, server, tmp_path):
    values = case(history, server, tmp_path, wall_ms=2100)
    control(values, "pause")
    ready_step(values)
    job = values[0].get(values[2], UUID(values[4].job_id))
    deadline = job["deadline_at"]
    limit = time.perf_counter() + 5
    while True:
        with history.engine.connect() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        if now >= deadline:
            break
        assert time.perf_counter() < limit
        time.sleep(0.02)
    control(values, "resume")
    assert (
        ready_step(values, a2a_client_factory=lambda _: pytest.fail("过期任务不能创建客户端"))[
            "status"
        ]
        == "budget_exhausted"
    )
    assert (
        state(values).usage.tool_calls == 0
        and values[0].get(values[2], UUID(values[4].job_id))["deadline_at"] == deadline
    )


def test_source_mismatch_is_rejected_before_client(history, server, tmp_path, monkeypatch):
    values = case(history, server, tmp_path)
    monkeypatch.setattr(
        "agentgit.runtime.a2a_run.source_revision", lambda: "source-sha256:" + "0" * 64
    )
    assert (
        ready_step(values, a2a_client_factory=lambda _: pytest.fail("源码不符不能创建客户端"))[
            "status"
        ]
        == "failed"
    )
    assert (
        values[0].run(values[2], UUID(values[4].run_id))["failure_code"]
        == ErrorCode.UNSUPPORTED_SCHEMA.value
    )
    assert values[6]()["accepted_messages"] == 0


def test_a2a_job_prevents_destructive_downgrade(history, server, tmp_path):
    case(history, server, tmp_path)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.attributes["testing"] = True
    with pytest.raises(RuntimeError, match="已有 A2A Job"), history.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0032_collab_recorded_steps")
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0033_a2a_runs"
        )


def test_cancel_reaffirmation_does_not_duplicate_or_bypass_approval(history, server, tmp_path):
    values = case(history, server, tmp_path, interval=20)
    values[5]("working")
    start(values)
    control(values, "cancel")
    assert ready_step(values)["status"] == "waiting_approval"
    control(values, "cancel")
    assert ready_step(values)["status"] == "waiting_approval"
    assert values[6]()["tasks"]["task-1"]["value"]["status"]["state"] == "TASK_STATE_WORKING"
    approve(values)
    assert ready_step(values)["status"] == "cancelled"
    assert len([r for r in values[6]()["requests"] if r["path"].endswith(":cancel")]) == 1


def test_pause_after_durable_intent_stays_unknown_without_sending(history, server, tmp_path):
    values = case(history, server, tmp_path)
    assert ready_step(values)["status"] == "waiting_approval"
    approve(values)

    def fault(point):
        if point == "a2a.after_intent":
            control(values, "pause")

    assert ready_step(values, fault=fault)["status"] == "outcome_unknown"
    assert state(values).pending is not None and values[6]()["accepted_messages"] == 0
    assert Worker(values[0]).run_once() is None


def test_auth_required_resume_queries_known_task_without_resending(history, server, tmp_path):
    values = case(history, server, tmp_path)
    values[5]("auth_required")
    assert start(values)["status"] == "paused" and state(values).waiting_for == "auth"
    values[5]("success")
    control(values, "resume")
    assert ready_step(values)["status"] == "completed"
    assert state(values).polls == 1 and values[6]()["accepted_messages"] == 1


def test_program_secret_is_rejected_before_any_new_cas_file(history, server, tmp_path):
    values = case(history, server, tmp_path)
    raw = values[3].model_dump(mode="json")
    body = copy.deepcopy(CASES["request"])
    secret = os.environ[values[3].binding.server.credential_env]
    body["message"]["parts"][0]["text"] = secret
    raw["messages"] = [encode(body).decode()]
    program = A2AProgram.model_validate_json(encode(raw))
    before = {
        p.relative_to(history.objects.root): p.read_bytes()
        for p in history.objects.root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(AgentGitError) as error:
        values[1].submit(
            values[2],
            program,
            actor=ACTOR,
            key="private",
            branch_name="main",
            expected_head=None,
            expected_version=0,
        )
    assert error.value.code == ErrorCode.FORBIDDEN and secret not in str(error.value)
    after = {
        p.relative_to(history.objects.root): p.read_bytes()
        for p in history.objects.root.rglob("*")
        if p.is_file()
    }
    assert after == before and all(secret.encode() not in data for data in after.values())


def test_empty_schema_a2a_migration_roundtrip_preserves_tables(history, server):
    setup(server)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.attributes["testing"] = True
    with history.engine.begin() as connection:
        config.attributes["connection"] = connection
        before = connection.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema()"
            )
        ).scalar_one()
        command.downgrade(config, "0032_collab_recorded_steps")
        command.upgrade(config, "head")
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema=current_schema()"
                )
            ).scalar_one()
            == before
        )
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0033_a2a_runs"
        )
