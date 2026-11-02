"""真实 PostgreSQL/独立 MCP 进程：策略、原始录制及零网络回放。"""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import sqlalchemy as sa

from agentgit.adapters.mcp import MCPAdapter, MCPClient
from agentgit.core.canonical import ObjectType, decode
from agentgit.core.effects import ToolPolicy
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.mcp import MCPServer
from agentgit.core.models import CallIntent, EffectClass, EnvironmentManifest, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.replay.recorded import RecordedAdapter, Recording
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, EventInput

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def archive(history, server, request):
    yield
    destination = os.environ.get("AGENTGIT_MCP_EVIDENCE_DIR")
    if destination is None:
        return
    config, counts, _ = server
    with history.engine.connect() as connection:
        tables = {
            name: [dict(row) for row in connection.execute(sa.select(TABLES[name])).mappings()]
            for name in (
                "objects",
                "object_refs",
                "repositories",
                "runs",
                "branches",
                "commits",
                "events",
                "external_calls",
                "approvals",
            )
        }
    objects = []
    for row in tables["objects"]:
        payload = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            {
                "id": row["id"],
                "type": row["object_type"],
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    packet = {
        "kind": "mcp_raw_calls/v1",
        "test": request.node.nodeid,
        "source_revision": source_revision(),
        "lock_digest": dependency_lock_digest(),
        "server": config.model_dump(mode="json"),
        "counts": counts(),
        "tables": tables,
        "objects": objects,
    }
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16] + ".json"
    with (folder / name).open("x", encoding="utf-8") as handle:
        json.dump(packet, handle, default=str, ensure_ascii=False, indent=2)


@pytest.fixture
def server(tmp_path):
    ready = tmp_path / "mcp-ready.json"
    helper = Path(__file__).parents[1] / "helpers/mcp_server.py"
    process = subprocess.Popen(
        [sys.executable, str(helper), str(ready)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        deadline = monotonic() + 10
        while True:
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode(errors="replace"))
            assert monotonic() < deadline, "MCP fixture 未就绪"
            try:
                endpoint = json.loads(ready.read_text("utf-8"))["endpoint"]
                break
            except (FileNotFoundError, PermissionError):
                # Windows 的短暂共享访问拒绝仍计入原就绪时限。
                Event().wait(0.02)
        opener = build_opener(ProxyHandler({}))

        def counts():
            with opener.open(endpoint.replace("/mcp", "/state"), timeout=2) as response:
                return json.loads(response.read(8192))

        def mode(value):
            with opener.open(
                Request(
                    endpoint.replace("/mcp", "/control"),
                    json.dumps({"mode": value}).encode(),
                    {"Content-Type": "application/json"},
                ),
                timeout=2,
            ) as response:
                assert response.status == 200

        yield (
            MCPServer(server_identity="trusted-fixture", endpoint=endpoint, timeout_ms=150),
            counts,
            mode,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def setup(history, server, effect=EffectClass.READ_ONLY, credential_env=None):
    config, counts, mode = server
    client = MCPClient(config, credential_env=credential_env)
    binding = client.bind("echo", effect)
    adapter = MCPAdapter(binding, client)
    repo = history.create_repository("MCP 精确工具")
    run = history.create_run(repo)
    environment = EnvironmentManifest(
        runtime_version="0.1.0",
        python_version="3.12",
        code_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        model_signature="no-model",
        prompt_digest="b" * 64,
        input_ref=history.register_input(repo, b"mcp-input"),
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )
    history.commit(
        repo,
        run,
        expected_head=None,
        expected_version=0,
        state={"fixture": "before-mcp"},
        environment=environment,
        events=(EventInput("fixture.ready", "mcp", {}),),
    )
    approval = ApprovalService(history, ToolPolicy(rules=(binding.rule(),)), actor="mcp-operator")

    def checkpoint():
        branch = history.branch(repo)
        return branch["head_commit_id"], branch["version"]

    gateway = RecordedCallGateway(history, repo, run, checkpoint, authorization=approval)
    return adapter, gateway, approval, repo, run, counts, mode


def call(adapter, gateway):
    return adapter.call(gateway, step="mcp-tool", attempt=1, arguments={"value": "fixture-value"})


def approve(history, values):
    adapter, gateway, approval, repo, run, counts, _ = values
    before = counts()
    with pytest.raises(AgentGitError) as caught:
        call(adapter, gateway)
    assert caught.value.code == ErrorCode.APPROVAL_REQUIRED
    assert counts() == before
    pending = approval.list_for_run(repo, run)[-1]
    with history.engine.connect() as connection:
        current = history._run(connection, repo, run)
    approval.decide(
        repo,
        pending["id"],
        actor="mcp-operator",
        decision="approve",
        expected_version=current["state_version"],
    )


def test_success_is_recorded_and_replayed_without_client_or_network(history, server):
    adapter, gateway, _, repo, run, counts, _ = setup(history, server)
    outcome = call(adapter, gateway)
    assert outcome.error is None
    payload = decode(outcome.payload)
    assert payload["server_identity"] == "trusted-fixture"
    assert payload["tool_name"] == "echo" and payload["effect_class"] == "READ_ONLY"
    assert payload["structuredContent"] == {"value": "fixture-value"}
    assert payload["schema_digest"] == adapter.binding.schema_digest()
    assert counts()["valid_headers"] and counts()["calls"] == 1
    before = counts()
    assert call(adapter, gateway) == outcome
    assert counts() == before
    with history.engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .one()
        )
    intent = parse_contract(history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent)
    saved = parse_contract(
        history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
    )
    request = history.objects.get(intent.request_ref, ObjectType.BLOB)
    response = history.objects.get(saved.response_ref, ObjectType.BLOB)
    recording = Recording(intent, saved, request, response, str(row["id"]))
    replay = RecordedAdapter((recording,), lambda: intent.checkpoint)
    # 回放不构造 MCPClient；即使工具后来改变，也只能消费确切原始录制。
    offline = MCPAdapter(adapter.binding)
    replayed = call(offline, replay)
    replay.assert_consumed()
    assert replayed.payload == outcome.payload and replayed.error is None
    assert counts() == before
    with pytest.raises(AgentGitError) as missing:
        call(offline, RecordedAdapter((), lambda: intent.checkpoint))
    assert missing.value.code == ErrorCode.RECORDING_MISSING
    assert counts() == before
    assert intent.occurrence.request_digest == payload["arguments_digest"]


def test_remote_readonly_hint_does_not_bypass_write_approval(history, server):
    values = setup(history, server, EffectClass.NON_IDEMPOTENT_WRITE)
    approve(history, values)
    adapter, gateway, approval, repo, run, counts, _ = values
    outcome = call(adapter, gateway)
    assert outcome.error is None and counts()["calls"] == 1
    assert call(adapter, gateway) == outcome and counts()["calls"] == 1
    assert approval.list_for_run(repo, run)[-1]["status"] == "consumed"


def test_schema_change_refuses_call_and_keeps_original_pin(history, server):
    adapter, gateway, _, _, _, counts, mode = setup(history, server)
    signature = adapter.binding.signature()
    mode("changed")
    outcome = call(adapter, gateway)
    assert outcome.error.code == ErrorCode.UNSUPPORTED_SCHEMA
    assert counts()["calls"] == 0 and adapter.binding.signature() == signature


@pytest.mark.parametrize(
    "mode_name",
    [
        "timeout",
        "drop",
        "redirect",
        "tool_error",
        "wrong_id",
        "input_required",
        "bad_output",
        "sse",
    ],
)
def test_sent_write_failure_stays_unknown_and_is_never_reissued(history, server, mode_name):
    values = setup(history, server, EffectClass.NON_IDEMPOTENT_WRITE)
    approve(history, values)
    adapter, gateway, _, _, _, counts, mode = values
    mode(mode_name)
    outcome = call(adapter, gateway)
    assert outcome.error.code == ErrorCode.OUTCOME_UNKNOWN
    assert counts()["calls"] == 1
    before = counts()
    with pytest.raises(AgentGitError) as repeated:
        call(adapter, gateway)
    assert repeated.value.code == ErrorCode.OUTCOME_UNKNOWN
    assert counts() == before


def test_credentials_never_enter_requests_results_or_error_history(history, server, monkeypatch):
    secret = "mcp-test-private-value-91a2"
    monkeypatch.setenv("AGENTGIT_TEST_MCP_KEY", secret)
    adapter, gateway, _, _, run, counts, mode = setup(
        history, server, credential_env="AGENTGIT_TEST_MCP_KEY"
    )
    mode("credential_echo")
    outcome = call(adapter, gateway)
    assert outcome.error.code == ErrorCode.TOOL_ERROR
    assert secret not in str(outcome.error) and secret.encode() not in outcome.payload
    assert counts()["authorized"]
    with history.engine.connect() as connection:
        rows = (
            connection.execute(
                sa.select(TABLES["external_calls"]).where(TABLES["external_calls"].c.run_id == run)
            )
            .mappings()
            .all()
        )
    for row in rows:
        for field in ("request_digest", "result_ref", "intent_ref"):
            kind = ObjectType.BLOB if field == "request_digest" else ObjectType.CALL
            assert secret.encode() not in history.objects.get(row[field], kind)
    assert secret not in adapter.binding.model_dump_json()
    before = counts()
    for value in ({"value": secret}, {"value": "ok", "api_key": "other-secret"}):
        with pytest.raises(AgentGitError) as caught:
            adapter.call(gateway, step="invalid", attempt=1, arguments=value)
        assert caught.value.code == ErrorCode.FORBIDDEN
    assert counts() == before


@pytest.mark.parametrize("mode_name", ["timeout", "tool_error", "bad_output", "input_required"])
def test_readonly_errors_are_local_standard_errors(history, server, mode_name):
    adapter, gateway, _, _, _, counts, mode = setup(history, server)
    mode(mode_name)
    result = call(adapter, gateway)
    assert result.error.code == ErrorCode.TOOL_ERROR
    assert counts()["calls"] == 1


def test_different_local_effect_cannot_reuse_readonly_policy(history, server):
    adapter, gateway, _, _, _, counts, _ = setup(history, server)
    local_write = MCPAdapter(
        adapter.binding.model_copy(update={"effect_class": EffectClass.NON_IDEMPOTENT_WRITE}),
        adapter.client,
    )
    before = counts()
    with pytest.raises(AgentGitError) as caught:
        call(local_write, gateway)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert counts() == before
