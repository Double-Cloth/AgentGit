"""真实TCP API重启与独立CLI／Worker进程共用同一A2A耐久任务。"""

import copy
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from a2a_runtime_evidence import archive_a2a_run as archive_a2a_run
from test_a2a_fixture import server as server
from test_a2a_operator import ACTOR, TOKEN
from test_a2a_operator import operator as operator

from agentgit.config import Settings, database_url
from agentgit.core.a2a_runs import A2AProgram
from agentgit.core.canonical import encode
from agentgit.services.environment import dependency_lock_digest, source_revision

pytestmark = pytest.mark.integration


def python_environment():
    return dict(
        os.environ,
        PYTHONIOENCODING="utf-8",
        PYTHONPATH=os.pathsep.join(
            (str(Path(__file__).parents[2] / "src"), str(Path(sa.__file__).resolve().parents[1]))
        ),
    )


@contextmanager
def tcp_api(manager, folder, proofs):
    with manager.history.engine.connect() as c:
        schema = c.execute(sa.text("SELECT current_schema()")).scalar_one()
    ready = folder / "ready.json"
    folder.mkdir()
    environment = dict(python_environment(), AGENTGIT_API_TOKEN=TOKEN, AGENTGIT_OPERATOR_ID=ACTOR)
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    script = Path(__file__).parents[1] / "helpers/api_process.py"
    with (folder / "api.log").open("wb") as output:
        process = subprocess.Popen(
            [
                interpreter,
                str(script),
                schema,
                str(manager.research.data_root),
                str(manager.history.objects.root),
                str(ready),
            ],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            until = time.perf_counter() + 25
            while True:
                assert process.poll() is None, "API在就绪前退出"
                assert time.perf_counter() < until, "API未就绪"
                try:
                    observed = json.loads(ready.read_text("utf-8"))
                    assert observed["pid"] == process.pid != os.getpid()
                    assert (
                        observed["source"] == source_revision()
                        and observed["lock"] == dependency_lock_digest()
                    )
                    url = f"http://127.0.0.1:{observed['port']}"
                    if httpx.get(url + "/health", timeout=0.5, trust_env=False).status_code == 200:
                        break
                except (FileNotFoundError, PermissionError, httpx.HTTPError):
                    pass
                time.sleep(0.02)
            yield url, schema
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            proofs.append(
                dict(
                    kind="api",
                    pid=process.pid,
                    exit_code=process.returncode,
                    source=source_revision(),
                    lock=dependency_lock_digest(),
                    schema=schema,
                )
            )


def cli(arguments, environment, proofs):
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    process = subprocess.Popen(
        [interpreter, "-c", "from agentgit.cli.main import app; app()", *arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        output, error = process.communicate(timeout=35)
    except BaseException:
        process.kill()
        process.wait(timeout=10)
        raise
    assert process.returncode == 0, error.decode("utf-8", errors="replace")
    result = json.loads(output)
    proofs.append(
        dict(
            kind="cli",
            pid=process.pid,
            exit_code=process.returncode,
            command=arguments[0],
            status=result.get("status"),
            source=source_revision(),
            lock=dependency_lock_digest(),
        )
    )
    return result


def test_tcp_restart_then_separate_cli_worker_and_full_recorded(operator, tmp_path):
    _, manager, repo, program, server = operator
    proofs = []
    binding_file = tmp_path / "example-binding.json"
    binding_file.write_bytes(encode(program.binding.model_dump(mode="json")))
    built = subprocess.run(
        [
            str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
            str(Path(__file__).parents[2] / "examples/a2a/prepare_program.py"),
            str(binding_file),
        ],
        env=python_environment(),
        capture_output=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert built.returncode == 0, built.stderr.decode("utf-8", errors="replace")
    program = A2AProgram.model_validate_json(built.stdout)
    headers = {"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)}
    request = dict(
        schema_version=1,
        program=program.model_dump(mode="json"),
        branch_name="main",
        expected_head=None,
        expected_version=0,
        replay=None,
    )
    with (
        tcp_api(manager, tmp_path / "first-api", proofs) as (url, schema),
        httpx.Client(base_url=url, headers=headers, timeout=15, trust_env=False) as client,
    ):
        response = client.post(
            f"/repositories/{repo}/a2a-runs",
            json=request,
            headers={"Idempotency-Key": "tcp-live"},
        )
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert (
            client.get(f"/repositories/{repo}/a2a-submissions", params={"key": "tcp-live"}).json()
            == receipt
        )
    configured = database_url(Settings.from_env().test_database_url).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    environment = dict(
        python_environment(),
        AGENTGIT_OPERATOR_ID=ACTOR,
        AGENTGIT_TEST_DATABASE_URL=configured.render_as_string(hide_password=False),
        AGENTGIT_TEST_DATA_ROOT=str(manager.research.data_root),
    )
    assert (
        cli(["worker", "--once", "--testing"], environment, proofs)["status"] == "waiting_approval"
    )
    with (
        tcp_api(manager, tmp_path / "second-api", proofs) as (url, _),
        httpx.Client(base_url=url, headers=headers, timeout=15, trust_env=False) as client,
    ):
        approvals = client.get("/approvals", params={"run_id": receipt["run_id"]}).json()["items"]
        assert len(approvals) == 1
        approval = approvals[0]
        response = client.post(
            f"/approvals/{approval['id']}/decision",
            json=dict(schema_version=1, decision="approve", expected_version=approval["version"]),
            headers={"Idempotency-Key": "tcp-approve"},
        )
        assert response.status_code == 202, response.text
        assert cli(["worker", "--once", "--testing"], environment, proofs)["status"] == "completed"
        live = client.get(f"/runs/{receipt['run_id']}").json()
        assert live["adapter"]["actual_attempts"] == 1
        spec = live["adapter"]["recording_candidate"]
        response = client.post(
            "/branches",
            json=dict(
                schema_version=1,
                source_run_id=receipt["run_id"],
                from_commit=spec["from_commit"],
                name="tcp-recorded",
                expected_version=0,
            ),
            headers={"Idempotency-Key": "tcp-fork"},
        )
        assert response.status_code == 201, response.text
        file, recording = tmp_path / "tcp-program.json", tmp_path / "tcp-recording.json"
        file.write_bytes(encode(program.model_dump(mode="json")))
        recording.write_bytes(encode(spec))
        counts = copy.deepcopy(server[2]())
        for name in server[3]["credential_envs"].values():
            environment.pop(name, None)
        target = cli(
            [
                "a2a-submit",
                str(repo),
                str(file),
                "--branch",
                "tcp-recorded",
                "--head",
                spec["from_commit"],
                "--expected-version",
                "0",
                "--key",
                "tcp-recorded",
                "--replay-file",
                str(recording),
                "--testing",
            ],
            environment,
            proofs,
        )
        assert (
            cli(
                ["a2a-submission", str(repo), "--key", "tcp-recorded", "--testing"],
                environment,
                proofs,
            )
            == target
        )
        for _ in range(4):
            result = cli(["worker", "--once", "--testing"], environment, proofs)
            if result["status"] == "completed":
                break
            assert result["status"] == "queued"
        else:
            pytest.fail("独立Recorded Worker未完成")
        view = client.get(f"/runs/{target['run_id']}").json()["adapter"]
        assert view["actual_attempts"] == view["actual_response_bytes"] == 0
        assert view["logical_state_ref"] == live["adapter"]["logical_state_ref"]
        assert view["task_id"] == live["adapter"]["task_id"]
        assert server[2]() == counts
    assert all(
        p["pid"] > 0 and p["pid"] != os.getpid() and p["exit_code"] is not None for p in proofs
    )
    (tmp_path / "operator-process.json").write_text(
        json.dumps(
            dict(
                processes=proofs,
                source_run_id=receipt["run_id"],
                recorded_run_id=target["run_id"],
                protocol_requests_before_recorded=len(counts["requests"]),
                protocol_requests_after_recorded=len(server[2]()["requests"]),
            )
        ),
        encoding="utf-8",
    )
