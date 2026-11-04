"""Recorded 来源消费／检查点／事务内的真实解释器终止与接管。"""

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from test_a2a_fixture import server as server
from test_a2a_recorded_runs import complete, current, recorded_case, step

from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "point",
    [
        "a2a.recorded.after_source",
        "a2a.recorded.before_checkpoint",
        "a2a.recorded.in_projection",
        "a2a.recorded.after_checkpoint",
        "a2a.recorded.after_release",
    ],
)
def test_actual_recorded_worker_death_consumes_each_source_call_once(
    history, server, tmp_path, point
):
    source, target, _ = recorded_case(history, server, tmp_path, "poll")
    assert step(target)["status"] == "queued"
    assert step(target)["status"] == "queued" and current(target).cursor == 1
    requests = copy.deepcopy(source[6]())
    with history.engine.connect() as c:
        schema = c.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "recorded-worker-marker.json"
    env = dict(os.environ)
    env.update(
        AGENTGIT_A2A_TEST_SCHEMA=schema,
        AGENTGIT_A2A_TEST_POINT=point,
        AGENTGIT_A2A_TEST_DATABASE=history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_A2A_TEST_OBJECTS=str(history.objects.root),
        AGENTGIT_A2A_TEST_DATA=str(tmp_path / "data"),
        AGENTGIT_A2A_TEST_MARKER=str(marker),
        PYTHONIOENCODING="utf-8",
        PYTHONPATH=os.pathsep.join(
            (str(Path(__file__).parents[2] / "src"), str(Path(sa.__file__).resolve().parents[1]))
        ),
    )
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    script = Path(__file__).parents[1] / "helpers/a2a_worker_process.py"
    with (tmp_path / "recorded-worker.log").open("wb") as output:
        process = subprocess.Popen(
            [interpreter, str(script)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            until = time.perf_counter() + 25
            while True:
                assert process.poll() is None, "Recorded Worker未到目标边界就退出"
                assert time.perf_counter() < until, "Recorded Worker未抵达目标边界"
                try:
                    observed = json.loads(marker.read_text("utf-8"))
                except (FileNotFoundError, PermissionError):
                    time.sleep(0.01)
                    continue
                break
            assert observed["pid"] == process.pid != os.getpid()
            assert (
                observed["source"] == source_revision()
                and observed["lock"] == dependency_lock_digest()
            )
            # 事务内注入时先终止释放真实锁，再读Job；不篡改租约或token。
            process.terminate()
            process.wait(timeout=10)
            assert process.returncode is not None and process.returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    before = target[0].get(target[2], UUID(target[4].job_id))
    before_cursor = current(target).cursor
    assert before_cursor == (
        2 if point in {"a2a.recorded.after_checkpoint", "a2a.recorded.after_release"} else 1
    )
    assert complete(target)["status"] == "completed"
    after = target[0].get(target[2], UUID(target[4].job_id))
    assert (
        after["fencing_token"] > before["fencing_token"]
        and after["deadline_at"] == before["deadline_at"]
    )
    assert current(target).cursor == 4 and current(target).logical_state.usage.tool_calls == 2
    assert current(target).actual_attempts == 0 and source[6]() == requests
    with history.engine.connect() as c:
        events, calls = TABLES["events"], TABLES["external_calls"]
        consumed = list(
            c.execute(
                sa.select(events).where(
                    events.c.run_id == UUID(target[4].run_id), events.c.type == "call.recorded"
                )
            ).mappings()
        )
        assert (
            c.execute(sa.select(calls.c.id).where(calls.c.run_id == UUID(target[4].run_id))).first()
            is None
        )
    assert len(consumed) == 2
    (tmp_path / "process-death.json").write_text(
        json.dumps(
            dict(
                kind="recorded_worker_death",
                marker=observed,
                pid=process.pid,
                exit_code=process.returncode,
                before_fence=before["fencing_token"],
                after_fence=after["fencing_token"],
                deadline_preserved=True,
                before_cursor=before_cursor,
                final_cursor=4,
                consumed_calls=2,
                requests_before=len(requests["requests"]),
                requests_after=len(source[6]()["requests"]),
            )
        ),
        encoding="utf-8",
    )
