"""真实 TCP API 重启后，独立 CLI Worker 使用同一耐久 Job。"""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from time import monotonic

import httpx
import pytest
import sqlalchemy as sa
from test_management_api import TOKEN, prepare
from test_management_api import api as api

from agentgit.config import Settings, database_url

pytestmark = pytest.mark.integration


@contextmanager
def server(service, ready):
    with service.history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    environment = {**os.environ, "AGENTGIT_API_TOKEN": TOKEN, "AGENTGIT_OPERATOR_ID": service.actor}
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "helpers/api_process.py"),
            schema,
            str(service.research.data_root),
            str(service.history.objects.root),
            str(ready),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        deadline, url = monotonic() + 15, None
        while monotonic() < deadline:
            assert process.poll() is None, process.stderr.read().decode(errors="replace")
            if ready.exists():
                url = "http://127.0.0.1:" + str(
                    json.loads(ready.read_text(encoding="utf-8"))["port"]
                )
                try:
                    if httpx.get(url + "/health", timeout=0.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
            Event().wait(0.05)
        else:
            pytest.fail("API 进程未就绪")
        yield url, schema
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def test_api_restart_then_separate_cli_worker_completes(api, tmp_path):
    _, service, settings = api
    with (
        server(service, tmp_path / "ready-first.json") as (url, schema),
        httpx.Client(
            base_url=url, headers={"Authorization": "Bearer " + TOKEN}, timeout=10
        ) as client,
    ):
        repo, _, _, submitted = prepare((client, service, settings))
        assert (
            client.get(f"/jobs/{submitted['job_id']}", headers={"X-Repository-ID": repo}).json()[
                "status"
            ]
            == "queued"
        )
    url_with_schema = database_url(Settings.from_env().test_database_url).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    environment = {
        **os.environ,
        "AGENTGIT_TEST_DATABASE_URL": url_with_schema.render_as_string(hide_password=False),
        "AGENTGIT_TEST_DATA_ROOT": str(service.research.data_root),
        "AGENTGIT_OPERATOR_ID": service.actor,
    }
    with server(service, tmp_path / "ready-second.json") as (url, _):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from agentgit.cli.main import app; app()",
                "worker",
                "--once",
                "--testing",
            ],
            capture_output=True,
            timeout=30,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert json.loads(result.stdout)["status"] == "completed", result.stdout
        with httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": repo},
            timeout=10,
        ) as client:
            job = client.get(f"/jobs/{submitted['job_id']}").json()
            assert job["status"] == "completed"
            assert job["budget"]["usage"]["model_calls"] == 11
            assert len(client.get(f"/runs/{submitted['run_id']}/artifacts").json()["items"]) == 2
