"""实际容器证据只读展示：二进制分段、失败/未知和无执行器读取。"""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_service import call, request, rows

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import CodeArguments
from agentgit.services.execution_views import ExecutionViewService
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]
TOKEN = "fixture-execution-view-token-0000000000000000"


def client_for(execution):
    manager = ManagementService(execution["queue"].research)
    return TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer " + TOKEN,
            "X-Repository-ID": str(execution["repository"]),
        },
    )


def test_real_shell_evidence_is_scoped_binary_and_read_without_backend(execution, monkeypatch):
    program = b"cat > copied-input\nprintf '\\377abc'\nprintf '<script>alert(1)</script>' >&2"
    assert call(execution, request(execution, program, stdin=b"in\x00\xff")).error is None
    row = rows(execution, "external_calls")[0]
    base = f"/runs/{execution['run']}/executions/{row['id']}"
    original = execution["history"].branch(execution["repository"])

    def forbidden(*args, **kwargs):
        pytest.fail("只读执行详情不能初始化或调用执行器")

    with monkeypatch.context() as offline:
        offline.setattr("agentgit.adapters.execution.engine.DockerEngine.__init__", forbidden)
        offline.setattr(
            "agentgit.adapters.execution.container.SandboxContainer.__init__", forbidden
        )
        offline.setattr("agentgit.services.executions.ExecutionService.__init__", forbidden)
        with client_for(execution) as client:
            listing = client.get(f"/runs/{execution['run']}/executions").json()
            assert listing["items"][0]["id"] == str(row["id"])
            detail = client.get(base)
            assert detail.status_code == 200, detail.text
            value = detail.json()
            assert value["physical_run_id"] == str(execution["run"])
            assert value["result"]["exit_code"] == 0 and value["cleanup_confirmed"]
            assert type(value["wall_ms"]) is int and value["wall_ms"] > 0
            assert client.get(base + "/content/command").content == program
            assert client.get(base + "/content/stdin").content == b"in\x00\xff"
            assert client.get(base + "/content/stdout").content == b"\xffabc"
            part = client.get(base + "/content/stdout", params={"offset": 1, "limit": 2})
            assert part.content == b"ab" and part.headers["X-Content-Bytes"] == "4"
            assert part.headers["X-Next-Offset"] == "3"
            assert part.headers["Content-Security-Policy"] == "sandbox"
            assert part.headers["X-Content-Type-Options"] == "nosniff"
            assert client.get(base + "/content/stderr").content == b"<script>alert(1)</script>"
            assert client.get(base + "/content/code").status_code == 404
            assert client.get(base + "/content/" + "a" * 64).status_code == 422
            assert client.get(base + "/content/stdout", params={"offset": 5}).status_code == 422
            assert (
                client.get(base + "/content/stdout", params={"limit": 1_048_577}).status_code == 422
            )
            assert client.get(base, headers={"X-Repository-ID": str(uuid4())}).status_code == 404
            with execution["history"].engine.begin() as connection:
                connection.execute(
                    TABLES["branches"]
                    .insert()
                    .values(repository_id=execution["repository"], name="other")
                )
            other_run = execution["history"].create_run(execution["repository"], "other")
            assert client.get(f"/runs/{other_run}/executions/{row['id']}").status_code == 404
    assert execution["history"].branch(execution["repository"]) == original


def test_failed_execution_has_logs_but_no_published_output(execution):
    outcome = call(
        execution, request(execution, b"printf diagnostic; printf partial > partial; exit 7")
    )
    assert outcome.error is not None
    identity = rows(execution, "external_calls")[0]["id"]
    with client_for(execution) as client:
        base = f"/runs/{execution['run']}/executions/{identity}"
        value = client.get(base).json()
        assert value["call_status"] == "failed" and value["result"]["exit_code"] == 7
        assert value["result"]["output_manifest_ref"] is None
        assert client.get(base + "/content/stdout").content == b"diagnostic"
        assert client.get(base + "/content/output_manifest").status_code == 404


def test_pending_execution_has_request_but_no_invented_result(execution):
    observed = []

    def before_create(point):
        if point == "sandbox_before_create":
            row = rows(execution, "external_calls")[0]
            service = ExecutionViewService(execution["history"])
            value = service.detail(execution["repository"], execution["run"], row["id"])
            observed.append(value)
            assert value["result"] is value["wall_ms"] is None
            assert "stdout" not in value["resources"]
            assert service.content(execution["repository"], execution["run"], row["id"], "command")[
                1
            ]

    execution["service"].fault = before_create
    assert call(execution, request(execution)).error is None
    assert len(observed) == 1


def test_python_script_content_comes_from_its_versioned_input(execution):
    code = b"from pathlib import Path\nprint(Path('seed').read_text())\n"
    manifest, _ = execution["service"].authorize_inputs(
        execution["repository"],
        execution["run"],
        execution["checkpoint"],
        {"work/program.py": code, "work/seed": b"versioned"},
    )
    arguments = CodeArguments(
        script_path="program.py",
        cwd="work",
        argv=("first",),
        input_manifest_ref=manifest,
        execution_policy_ref=execution["policy"].digest(),
    )
    assert call(execution, arguments, provider="tool:code_exec/v1").error is None
    identity = rows(execution, "external_calls")[0]["id"]
    with client_for(execution) as client:
        base = f"/runs/{execution['run']}/executions/{identity}"
        assert client.get(base + "/content/code").content == code
        value = client.get(base).json()
        assert value["arguments"]["cwd"] == "work" and value["arguments"]["argv"] == ["first"]
        assert client.get(base + "/content/stdout").content == b"versioned\n"


@pytest.mark.parametrize("target", ["stdout", "metadata", "command"])
def test_missing_evidence_fails_without_fallback(execution, target):
    assert call(execution, request(execution)).error is None
    row = rows(execution, "sandbox_executions")[0]
    saved = execution["history"].objects.get_json(row["response_ref"], ObjectType.BLOB)
    digest = {
        "stdout": saved["stdout_ref"],
        "metadata": row["metadata_ref"],
        "command": saved["command_ref"],
    }[target]
    path = execution["history"].objects.path_for(digest)
    assert path.is_relative_to(execution["history"].objects.root)
    path.unlink()
    with client_for(execution) as client:
        response = client.get(f"/runs/{execution['run']}/executions/{row['call_id']}")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "recording_missing"


def test_reused_execution_points_to_original_physical_run(history, tmp_path, image_id):
    from test_agent_flow import submit
    from test_stopped_retries import expire

    from agentgit.worker import Worker

    queue, repo, job = submit(history, tmp_path)

    def crash(point):
        if point == "after_result":
            with history.engine.connect() as connection:
                count = connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["sandbox_executions"])
                ).scalar_one()
            if count:
                raise RuntimeError("已保存物理结果")

    with pytest.raises(RuntimeError, match="已保存物理结果"):
        Worker(queue, fault=crash).run_once()
    expire(queue, job)
    assert Worker(queue).run_once()["status"] == "completed"
    current = queue.get(repo, UUID(job.job_id))
    assert current["run_id"] != UUID(job.run_id)
    manager = ManagementService(queue.research)
    listing = manager.executions(repo, current["run_id"])
    values = [
        manager.execution(repo, current["run_id"], UUID(item["id"])) for item in listing["items"]
    ]
    reused = next(item for item in values if item["physical_run_id"] == job.run_id)
    assert reused["physical_call_id"] != reused["call_id"]
    assert (
        manager.execution_content(repo, current["run_id"], UUID(reused["call_id"]), "stdout")[1]
        == b"statistics written\n"
    )
    first = manager.executions(repo, current["run_id"], limit=1)
    second = manager.executions(
        repo, current["run_id"], after_id=UUID(first["next_after_id"]), limit=1
    )
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {
        item["id"] for item in listing["items"]
    }
    assert second["next_after_id"] is None
    with pytest.raises(AgentGitError) as error:
        manager.executions(repo, current["run_id"], after_id=UUID(reused["physical_call_id"]))
    assert error.value.code == ErrorCode.NOT_FOUND


def test_unknown_cleanup_exposes_diagnostic_without_publishing_files(execution, monkeypatch):
    from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer

    row = None
    try:
        with monkeypatch.context() as uncertain:
            uncertain.setattr(SandboxContainer, "cleanup", lambda self: False)
            outcome = call(
                execution, request(execution, b"printf diagnostic; printf data > partial")
            )
        assert outcome.error is not None and outcome.error.code == ErrorCode.OUTCOME_UNKNOWN
        row = rows(execution, "sandbox_executions")[0]
        with client_for(execution) as client:
            base = f"/runs/{execution['run']}/executions/{row['call_id']}"
            response = client.get(base)
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["execution_status"] == value["call_status"] == "outcome_unknown"
            assert not value["cleanup_confirmed"] and value["result"]["output_manifest_ref"] is None
            assert value["result"]["error_code"] == "outcome_unknown"
            assert client.get(base + "/content/stdout").content == b"diagnostic"
            assert client.get(base + "/content/output_manifest").status_code == 404
    finally:
        if row is not None:
            container = SandboxContainer(
                execution["engine"],
                row["image_id"],
                ExecutionIdentity(
                    str(row["run_id"]), str(row["call_id"]), row["attempt"], row["fencing_token"]
                ),
                row["effective_timeout_ms"],
            )
            assert container.cleanup()
