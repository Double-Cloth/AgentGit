"""相同耐久任务契约在真实批准 stdio 程序上运行，不复制恢复断言。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
import test_mcp_runs as runtime_cases
from mcp_evidence import archive_mcp_run as archive_mcp_run

from agentgit.adapters.mcp import MCPClient
from agentgit.core.models import EffectClass
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def stdio_server_case(mcp_stdio):
    config, _, control, records, exited = mcp_stdio

    def counts():
        rows = records()
        requests = [r for r in rows if r["event"] == "request"]
        starts = [r for r in rows if r["event"] == "started"]
        calls = len([r for r in rows if r["event"] == "effect"])
        return {
            "mode": control.read_text("utf-8") if control.is_file() else "normal",
            "requests": len(requests),
            "calls": calls,
            "writes": calls,
            "valid_headers": all(
                r["meta"].get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
                and "io.modelcontextprotocol/clientCapabilities" in r["meta"]
                for r in requests
            ),
            "authorized": False,
            "processes": len(starts),
            "records": rows,
            "all_exited": all(exited(r["pid"]) for r in starts),
        }

    return config, counts, lambda mode: control.write_text(mode, encoding="utf-8")


@pytest.fixture
def server(mcp_stdio):
    value = stdio_server_case(mcp_stdio)
    yield value
    assert value[1]()["all_exited"], "stdio 自有进程必须在每次协议请求后完成清理"


test_stdio_success_and_recorded = (
    runtime_cases.test_worker_success_and_recorded_preserve_artifact_and_logical_projection
)
test_stdio_queued_cancel = runtime_cases.test_queued_cancel_does_not_construct_client_or_call
test_stdio_wrong_recorded_mode = (
    runtime_cases.test_recorded_mode_without_fixed_recording_fails_before_client
)
test_stdio_pause_and_resume = runtime_cases.test_queued_pause_and_resume_execute_only_once
test_stdio_submission_identity = (
    runtime_cases.test_submission_key_and_branch_writer_cannot_be_rebound
)
test_stdio_unknown_cancel = runtime_cases.test_unknown_does_not_requeue_or_reissue_after_cancel
test_stdio_expired = runtime_cases.test_expired_job_has_zero_calls_even_when_resumed
test_stdio_process_death = runtime_cases.test_actual_worker_process_death_never_reissues_write
test_stdio_lost_lease = runtime_cases.test_lost_lease_between_discovery_and_call_prevents_write
test_stdio_missing_recording = (
    runtime_cases.test_missing_recorded_response_fails_without_live_fallback
)
test_stdio_cross_repository = runtime_cases.test_recorded_source_cannot_cross_repository


def test_lease_lost_between_stdio_discover_and_tool_never_sends_write(
    history, server, mcp_stdio, tmp_path
):
    queue, _, repo, program, submitted, counts, _ = runtime_cases.case(
        history, server, tmp_path, EffectClass.NON_IDEMPOTENT_WRITE
    )
    run = UUID(submitted.run_id)
    assert Worker(queue).run_once()["status"] == "waiting_approval"
    runtime_cases.approve(history, queue, repo, run, program)
    injected = []

    class FencedClient(MCPClient):
        def guard_requests(self, guard):
            def fenced_guard():
                requests = [r for r in mcp_stdio[3]() if r["event"] == "request"]
                if (
                    not injected
                    and requests[-1]["method"] == "server/discover"
                    and sum(r["method"] == "tools/list" for r in requests) == 2
                ):
                    with history.engine.begin() as connection:
                        jobs = TABLES["jobs"]
                        connection.execute(
                            jobs.update()
                            .where(jobs.c.id == UUID(submitted.job_id))
                            .values(fencing_token=jobs.c.fencing_token + 1, worker="other-worker")
                        )
                    injected.append(requests[-1]["pid"])
                guard()

            super().guard_requests(fenced_guard)

    result = Worker(
        queue, mcp_client_factory=lambda binding: FencedClient(binding.server)
    ).run_once()
    assert result["status"] == "lease_lost" and len(injected) == 1
    assert counts()["calls"] == 0 and counts()["requests"] == 5 and counts()["processes"] == 3
    assert counts()["all_exited"]
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    before = counts()
    assert (
        Worker(
            queue, mcp_client_factory=lambda _: pytest.fail("失租后的不完整意图不能重发")
        ).run_once()["status"]
        == "outcome_unknown"
    )
    assert counts() == before
