"""合并新任务 HTTP/CLI 输入和只读重开；复用实际后端与 Worker。"""

import json
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from merge_proposal_cases import archive
from merge_proposal_cases import merge_case as merge_case
from pydantic import SecretStr
from test_merge_runs import approved, fork_and_request
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_merged_fork_run_http_cli_and_readonly_provenance(
    history, merge_case, tmp_path, monkeypatch, request
):
    case = approved(merge_case)
    repo, _, manager, _, _, _ = case
    fork, payload = fork_and_request(case)

    @contextmanager
    def management(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", management)
    token = "merged-run-local-api-fixture-1234567890"
    with TestClient(
        create_app(Settings(api_token=SecretStr(token)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(repo)},
    ) as client:
        branch = client.post(
            "/merge-branches",
            json=fork.model_dump(mode="json"),
            headers={"Idempotency-Key": "fork-merged-work"},
        )
        assert branch.status_code == 201, branch.text
        runner = CliRunner()

        def invoke(command, value, key):
            path = tmp_path / (command + ".json")
            path.write_bytes(encode(value))
            result = runner.invoke(app, [command, str(repo), "--file", str(path), "--key", key])
            assert result.exit_code == 0, result.output
            return json.loads(result.stdout)

        assert (
            invoke("merge-fork", fork.model_dump(mode="json"), "fork-merged-work") == branch.json()
        )
        assert (
            client.post(
                "/merge-runs",
                json=payload.model_dump(mode="json"),
                headers={"Idempotency-Key": "wrong-project", "X-Repository-ID": str(uuid4())},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/merge-runs",
                json={**payload.model_dump(mode="json"), "initial_state_ref": "a" * 64},
                headers={"Idempotency-Key": "uploaded-state"},
            ).status_code
            == 422
        )
        started = client.post(
            "/merge-runs",
            json=payload.model_dump(mode="json"),
            headers={"Idempotency-Key": "start"},
        )
        assert started.status_code == 202, started.text
        created = started.json()
        assert invoke("merge-run", payload.model_dump(mode="json"), "start") == created
        assert Worker(manager.jobs).run_once()["status"] == "completed"

        def forbid(*args, **kwargs):
            pytest.fail("重开查询不能写入 CAS")

        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbid)
            job = client.get("/jobs/" + created["job_id"])
            run = client.get("/runs/" + created["run_id"])
            assert job.status_code == run.status_code == 200
            assert job.json()["merged_input"] == run.json()["merged_input"]
            assert run.json()["merged_input"]["from_commit"] == payload.expected_head
        assert invoke("merge-run", payload.model_dump(mode="json"), "start") == created
        assert manager.jobs.get(repo, UUID(created["job_id"]))["status"] == "completed"
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(created=created, run=run.json(), job=job.json()),
    )
