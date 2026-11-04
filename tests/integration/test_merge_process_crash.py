"""实际终止受信任的测试子进程，核对数据库回滚/持久与 Worker 接管。"""

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from merge_proposal_cases import archive, history_fingerprint, rows_for
from merge_proposal_cases import merge_case as merge_case
from test_merge_approval import prepare
from test_merge_runs import approved, fork_and_request, source_unchanged

from agentgit.services.environment import source_revision
from agentgit.services.jobs import JobService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def kill_interpreter(pid):
    """Windows venv 启动器可能多一层；终止已核验父子关系的实际解释器。"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100001, False, pid)
    assert handle, ctypes.get_last_error()
    try:
        assert kernel.TerminateProcess(handle, 73), ctypes.get_last_error()
        assert kernel.WaitForSingleObject(handle, 10000) == 0
    finally:
        assert kernel.CloseHandle(handle)


def kill_at(history, research, tmp_path, *, mode, point, payload=None):
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    marker = tmp_path / "child-boundary.json"
    env = os.environ.copy()
    env.update(
        AGENTGIT_MERGE_CRASH_SCHEMA=schema,
        AGENTGIT_MERGE_CRASH_DATABASE=history.engine.url.render_as_string(hide_password=False),
        AGENTGIT_MERGE_CRASH_OBJECTS=str(history.objects.root),
        AGENTGIT_MERGE_CRASH_DATA=str(research.data_root),
        AGENTGIT_MERGE_CRASH_MARKER=str(marker),
        AGENTGIT_MERGE_CRASH_MODE=mode,
        AGENTGIT_MERGE_CRASH_POINT=point,
    )
    if payload is not None:
        data = tmp_path / "child-request.json"
        data.write_text(json.dumps(payload), "utf-8")
        env["AGENTGIT_MERGE_CRASH_REQUEST"] = str(data)
    helper = Path(__file__).resolve().parents[1] / "helpers/merge_crash_process.py"
    process = subprocess.Popen(
        [sys.executable, str(helper)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 25
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), (
            process.communicate(timeout=3)[0][-2000:].decode("utf-8", errors="replace")
            if process.poll() is not None
            else "子进程未到达指定边界"
        )
        record = json.loads(marker.read_text("utf-8"))
        assert record["source_revision"] == source_revision()
        assert record["pid"] == process.pid or (
            os.name == "nt" and record["parent_pid"] == process.pid
        )
        assert record["point"] == point and record["mode"] == mode and process.poll() is None
        if os.name == "nt":
            kill_interpreter(record["pid"])
        else:
            process.kill()
        output, _ = process.communicate(timeout=10)
        assert process.returncode != 0
        record.update(
            exit_code=process.returncode,
            launcher_pid=process.pid,
            captured_output_bytes=len(output),
        )
        return record
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.parametrize("point", ["merge.head_updated", "merge.after_commit"])
def test_approval_process_kill_preserves_atomicity_and_original_key(
    history, merge_case, tmp_path, request, point
):
    repo, research, manager, pins, _ = merge_case
    proposal, _, _, _, approval = prepare(manager.merge_proposals, repo, pins, actor=manager.actor)
    before = history_fingerprint(history, repo)
    marker = kill_at(
        history,
        research,
        tmp_path,
        mode="approve",
        point=point,
        payload=dict(
            repository_id=str(repo),
            proposal_id=str(proposal),
            request=approval.model_dump(mode="json"),
            actor=manager.actor,
            key="approve",
        ),
    )
    if point == "merge.head_updated":
        assert history_fingerprint(history, repo) == before
    else:
        assert manager.merge_proposal(repo, proposal)["status"] == "merged"
    result = manager.approve_merge_proposal(repo, proposal, approval, "approve")
    assert manager.approve_merge_proposal(repo, proposal, approval, "approve") == result
    rows = rows_for(history, repo)
    assert len(rows["merge_approvals"]) == 1
    assert history.branch(repo, pins.ours.name)["version"] == pins.ours.version + 1
    archive(
        history, repo, tmp_path, request.node.name + "/final", dict(result=result, process=marker)
    )


@pytest.mark.parametrize("point", ["before_transaction", "after_commit"])
def test_merged_run_process_kill_recovers_without_reusing_source_calls(
    history, merge_case, tmp_path, request, point
):
    case = approved(merge_case)
    repo, research, manager, _, _, _ = case
    _, payload = fork_and_request(case)
    created = manager.start_merged_run(repo, payload, "start")
    before = rows_for(history, repo)
    marker = kill_at(history, research, tmp_path, mode="run", point=point)
    stopped = rows_for(history, repo)
    assert stopped["external_calls"] == before["external_calls"]
    time.sleep(0.8)
    recovered = Worker(JobService(research, lease_ms=500)).run_once()
    assert recovered is not None and recovered["status"] == "completed"
    job = manager.jobs.get(repo, UUID(created["job_id"]))
    assert job["run_id"] != UUID(created["run_id"])
    rows = rows_for(history, repo)
    source_unchanged(before, rows)
    assert len(rows["merge_run_requests"]) == 1
    assert manager.start_merged_run(repo, payload, "start") == created
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(created=created, recovered=recovered, process=marker),
    )
