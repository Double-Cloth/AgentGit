"""实际解释器终止后的协议请求、调度补偿、fencing及预算核对。"""

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from test_a2a_fixture import server as server
from test_a2a_runs import approve, case, control, ready_step, state

from agentgit.services.environment import dependency_lock_digest, source_revision

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "point,mode,request_pause",
    [
        ("a2a.after_intent", "success", False),
        ("a2a.after_intent", "success", True),
        ("a2a.after_result", "success", False),
        ("a2a.after_observation", "success", False),
        ("a2a.after_observation", "working", False),
        ("a2a.after_defer", "working", False),
    ],
)
def test_actual_worker_death_never_reissues_sent_or_unconfirmed_write(
    history, server, tmp_path, point, mode, request_pause
):
    values = case(history, server, tmp_path, interval=100)
    assert ready_step(values)["status"] == "waiting_approval"
    approve(values)
    values[5](mode)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "worker-ready.json"
    env = dict(os.environ)
    env.update(
        AGENTGIT_A2A_TEST_SCHEMA=schema,
        AGENTGIT_A2A_TEST_POINT=point,
        AGENTGIT_A2A_TEST_DATABASE=history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_A2A_TEST_OBJECTS=str(history.objects.root),
        AGENTGIT_A2A_TEST_DATA=str(tmp_path / "data"),
        AGENTGIT_A2A_TEST_MARKER=str(marker),
        PYTHONPATH=os.pathsep.join(
            (str(Path(__file__).parents[2] / "src"), str(Path(sa.__file__).resolve().parents[1]))
        ),
    )
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    script = Path(__file__).parents[1] / "helpers/a2a_worker_process.py"
    with (tmp_path / "worker.log").open("wb") as output:
        process = subprocess.Popen(
            [interpreter, str(script)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.perf_counter() + 20
            while not marker.exists():
                assert process.poll() is None, "A2A Worker 在目标边界前退出"
                assert time.perf_counter() < deadline, "A2A Worker 未到达目标边界"
                time.sleep(0.01)
            observed = json.loads(marker.read_text("utf-8"))
            assert observed["pid"] == process.pid != os.getpid()
            assert (
                observed["source"] == source_revision()
                and observed["lock"] == dependency_lock_digest()
            )
            before = values[0].get(values[2], UUID(values[4].job_id))
            calls_before = len(values[6]()["requests"])
            if request_pause:
                control(values, "pause")
            process.terminate()
            process.wait(timeout=10)
            assert process.returncode is not None and process.returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    expected = "outcome_unknown" if point == "a2a.after_intent" else "completed"
    if mode == "working":
        values[5]("success")
    kwargs = (
        {}
        if point == "a2a.after_defer"
        else {"a2a_client_factory": lambda _: pytest.fail("接管应先消费结果或补调度，不创建客户端")}
    )
    first = ready_step(values, **kwargs)
    if mode == "working" and point == "a2a.after_observation":
        assert first["status"] == "queued" and len(values[6]()["requests"]) == calls_before
        first = ready_step(values)
    assert first["status"] == expected
    after = values[0].get(values[2], UUID(values[4].job_id))
    assert (
        after["fencing_token"] > before["fencing_token"]
        and after["deadline_at"] == before["deadline_at"]
    )
    assert values[6]()["accepted_messages"] == (0 if point == "a2a.after_intent" else 1)
    if expected == "outcome_unknown":
        assert state(values).pending is not None
        assert len(values[6]()["requests"]) == calls_before
    elif mode == "success":
        assert state(values).usage.tool_calls == 1
    else:
        assert state(values).usage.tool_calls == 2 and state(values).polls == 1
    (tmp_path / "process-death.json").write_text(
        json.dumps(
            dict(
                marker=observed,
                pid=process.pid,
                exit_code=process.returncode,
                before_fence=before["fencing_token"],
                after_fence=after["fencing_token"],
                deadline_preserved=True,
                requests_before=calls_before,
                requests_after=len(values[6]()["requests"]),
                result=first,
            )
        ),
        encoding="utf-8",
    )


def test_live_stale_worker_cannot_send_after_actual_fence_takeover(history, server, tmp_path):
    values = case(history, server, tmp_path)
    assert ready_step(values)["status"] == "waiting_approval"
    approve(values)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker, release, result_path = (
        tmp_path / name for name in ("stale-marker.json", "release.flag", "stale-result.json")
    )
    env = dict(os.environ)
    env.update(
        AGENTGIT_A2A_TEST_SCHEMA=schema,
        AGENTGIT_A2A_TEST_POINT="a2a.after_intent",
        AGENTGIT_A2A_TEST_DATABASE=history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_A2A_TEST_OBJECTS=str(history.objects.root),
        AGENTGIT_A2A_TEST_DATA=str(tmp_path / "data"),
        AGENTGIT_A2A_TEST_MARKER=str(marker),
        AGENTGIT_A2A_TEST_RELEASE=str(release),
        AGENTGIT_A2A_TEST_RESULT=str(result_path),
        PYTHONPATH=os.pathsep.join(
            (str(Path(__file__).parents[2] / "src"), str(Path(sa.__file__).resolve().parents[1]))
        ),
    )
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    script = Path(__file__).parents[1] / "helpers/a2a_worker_process.py"
    with (tmp_path / "stale-worker.log").open("wb") as output:
        process = subprocess.Popen(
            [interpreter, str(script)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            end = time.perf_counter() + 20
            while not marker.exists():
                assert process.poll() is None and time.perf_counter() < end
                time.sleep(0.01)
            observed = json.loads(marker.read_text("utf-8"))
            assert observed["pid"] == process.pid
            before = values[0].get(values[2], UUID(values[4].job_id))
            # 用真实行锁令心跳超时与租约自然过期，不直接改写 token 或状态。
            with history.engine.begin() as connection:
                connection.execute(
                    sa.text("SELECT id FROM jobs WHERE id=:id FOR UPDATE"),
                    {"id": UUID(values[4].job_id)},
                )
                until = before["lease_until"] + timedelta(milliseconds=2200)
                while (
                    connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one() <= until
                ):
                    assert process.poll() is None
                    time.sleep(0.05)
            assert (
                ready_step(
                    values, a2a_client_factory=lambda _: pytest.fail("新持有者不能重发未决意图")
                )["status"]
                == "outcome_unknown"
            )
            assert process.poll() is None
            release.write_text("resume", encoding="ascii")
            process.wait(timeout=10)
            assert process.returncode == 0
            result = json.loads(result_path.read_text("utf-8"))
            assert result["status"] == "lease_lost"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    after = values[0].get(values[2], UUID(values[4].job_id))
    assert (
        after["fencing_token"] > before["fencing_token"]
        and after["deadline_at"] == before["deadline_at"]
    )
    assert values[6]()["accepted_messages"] == 0 and len(values[6]()["requests"]) == 1
    (tmp_path / "process-death.json").write_text(
        json.dumps(
            dict(
                kind="live_stale_worker",
                marker=observed,
                pid=process.pid,
                exit_code=process.returncode,
                old_pid_alive_during_reclaim=True,
                before_fence=before["fencing_token"],
                after_fence=after["fencing_token"],
                result=result,
            )
        ),
        encoding="utf-8",
    )
