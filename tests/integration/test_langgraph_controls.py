"""在原生 fetch/review 完整边界落实控制，不丢录制、不重发或修改来源。"""

from uuid import UUID

import pytest
import sqlalchemy as sa
from test_langgraph_forks import fingerprint
from test_langgraph_recorded import checkpoints, finished, request
from test_langgraph_runs import archive as archive
from test_langgraph_runs import case, control, forbidden, state
from test_mcp_adapter import server as http_server  # noqa: F401 - pytest 按名称读取 fixture
from test_mcp_stdio_runs import stdio_server_case

from agentgit.core.models import RunStatus
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture(params=["http", "stdio"])
def server(request):
    if request.param == "http":
        yield request.getfixturevalue("http_server")
    else:
        value = stdio_server_case(request.getfixturevalue("mcp_stdio"))
        yield value
        assert value[1]()["all_exited"]


def inject(queue, history, repo, receipt, phase, action):
    injected = []

    def fault(point):
        if point != "langgraph.after_checkpoint" or injected:
            return
        current, _, _ = state(history, repo, UUID(receipt.run_id))
        if current.values.get("node_order") != (
            ["fetch"] if phase == "fetch" else ["fetch", "review"]
        ):
            return
        injected.append(current.logical_projection())
        if action == "deadline":
            with history.engine.begin() as connection:
                jobs = TABLES["jobs"]
                connection.execute(
                    jobs.update()
                    .where(jobs.c.id == UUID(receipt.job_id))
                    .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
                )
        else:
            control(queue, repo, UUID(receipt.run_id), action)

    return fault, injected


@pytest.mark.parametrize("phase", ["fetch", "review"])
@pytest.mark.parametrize("action", ["pause", "cancel", "deadline"])
def test_live_native_control_keeps_known_result_and_safe_boundary(
    history, server, tmp_path, phase, action
):
    queue, _, repo, _, receipt, counts, _ = case(history, server, tmp_path, pause=False)
    fault, injected = inject(queue, history, repo, receipt, phase, action)
    result = Worker(queue, fault=fault).run_once()
    assert (
        result["status"]
        == {"pause": "paused", "cancel": "cancelled", "deadline": "budget_exhausted"}[action]
    ), result
    current, snapshot, _ = state(history, repo, UUID(receipt.run_id))
    assert len(injected) == 1 and current.trace is not None and not current.pending
    assert current.usage.steps == (1 if phase == "fetch" else 2) and current.usage.tool_calls == 1
    assert current.native_next == (("review",) if phase == "fetch" else ("publish",))
    assert current.actual_attempts == 1 and counts()["calls"] == 1
    assert {a.path for a in snapshot.artifacts} == {"langgraph-native.json"}
    before = counts()
    if action == "pause":
        control(queue, repo, UUID(receipt.run_id), "resume")
        assert Worker(queue, mcp_client_factory=forbidden).run_once()["status"] == "completed"
    else:
        assert Worker(queue, mcp_client_factory=forbidden).run_once() is None
    assert counts() == before


@pytest.mark.parametrize("phase", ["fetch", "review"])
@pytest.mark.parametrize("action", ["pause", "cancel", "deadline"])
def test_recorded_native_control_is_independent_and_can_record_resumed_range(
    history, server, tmp_path, phase, action
):
    queue, service, repo, source, expected, last, counts = finished(
        history, server, tmp_path, pause=False
    )
    first = checkpoints(history, repo, source)[0][0]
    before_source, before_counts = fingerprint(history, queue, repo, source), counts()
    receipt = service.recorded(
        repo, request(source, first, last), actor="graph-operator", key="controlled-recorded"
    )
    fault, injected = inject(queue, history, repo, receipt, phase, action)
    result = Worker(queue, fault=fault, mcp_client_factory=forbidden).run_once()
    assert (
        result["status"]
        == {"pause": "paused", "cancel": "cancelled", "deadline": "budget_exhausted"}[action]
    ), result
    run = UUID(receipt.run_id)
    current, _, _ = state(history, repo, run)
    assert len(injected) == 1 and current.actual_attempts == 0 and current.trace == expected.trace
    assert current.usage.steps == (1 if phase == "fetch" else 2) and current.usage.tool_calls == 1
    if action == "pause":
        control(queue, repo, run, "resume")
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
        assert result["status"] == "completed", result
        current, _, row = state(history, repo, run)
        assert current.logical_projection() == expected.logical_projection()
        nested = service.recorded(
            repo,
            request(run, first, row["checkpoint_commit_id"], "nested-controlled"),
            actor="graph-operator",
            key="nested-controlled",
        )
        result = Worker(queue, mcp_client_factory=forbidden).run_once()
        assert result["status"] == "completed", result
        assert (
            state(history, repo, UUID(nested.run_id))[0].logical_projection()
            == expected.logical_projection()
        )
    else:
        assert current.status in {RunStatus.CANCELLED, RunStatus.BUDGET_EXHAUSTED}
        assert Worker(queue, mcp_client_factory=forbidden).run_once() is None
    assert fingerprint(history, queue, repo, source) == before_source and counts() == before_counts
