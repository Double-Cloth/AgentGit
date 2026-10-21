"""容器工具必须经过耐久意图、授权输入、真实执行与原子结果保存。"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import pytest
import sqlalchemy as sa

from agentgit.adapters.execution.engine import DockerEngine, EngineError
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import (
    CodeArguments,
    ExecutionManifest,
    ExecutionResult,
    TerminalArguments,
)
from agentgit.core.models import EffectClass, parse_contract
from agentgit.services.executions import ExecutionService
from agentgit.services.history import TABLES

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


def request(
    execution,
    program=b"printf hello > result; printf out; printf err >&2",
    *,
    files=None,
    stdin=None,
    timeout=30000,
):
    blobs = (program,) if stdin is None else (program, stdin)
    manifest, refs = execution["service"].authorize_inputs(
        execution["repository"], execution["run"], execution["checkpoint"], files or {}, blobs=blobs
    )
    return TerminalArguments(
        command_ref=refs[0],
        stdin_ref=refs[1] if stdin is not None else None,
        input_manifest_ref=manifest,
        execution_policy_ref=execution["policy"].digest(),
        timeout_ms=timeout,
    )


def call(execution, arguments, *, provider="tool:terminal_exec/v1", gateway=None, step="execute"):
    return (gateway or execution["gateway"]()).call(
        step=step,
        attempt=1,
        provider=provider,
        signature=execution["service"].signature(provider),
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        request=encode(arguments.model_dump(mode="json")),
        invoke=lambda: pytest.fail("容器调用不能回退到普通 invoke"),
        max_bytes=16384,
    )


def rows(execution, name):
    with execution["history"].engine.connect() as connection:
        return list(connection.execute(sa.select(TABLES[name])).mappings())


def result(execution, outcome):
    return parse_contract(outcome.payload, ExecutionResult)


def test_intent_is_committed_before_real_container_creation(execution):
    observed = []

    def fault(point):
        if point == "sandbox_before_create":
            observed.append(
                (
                    len(rows(execution, "external_calls")),
                    rows(execution, "sandbox_executions")[0]["status"],
                )
            )

    execution["service"].fault = fault
    outcome = call(execution, request(execution, files={"input": b"original"}))
    assert outcome.error is None, outcome.error
    assert observed == [(1, "intent")]
    saved = result(execution, outcome)
    assert saved.exit_code == 0 and saved.cleanup_confirmed and saved.integrity_complete
    assert execution["history"].objects.get(saved.stdout_ref, ObjectType.BLOB) == b"out"
    assert execution["history"].objects.get(saved.stderr_ref, ObjectType.BLOB) == b"err"
    manifest = parse_contract(
        execution["history"].objects.get(saved.output_manifest_ref, ObjectType.ARTIFACT_MANIFEST),
        ExecutionManifest,
    )
    files = {
        item.path: execution["history"].objects.get(item.object_ref, ObjectType.BLOB)
        for item in manifest.files
    }
    assert files == {"input": b"original", "result": b"hello"}
    row = rows(execution, "sandbox_executions")[0]
    external = rows(execution, "external_calls")[0]
    assert row["status"] == "complete" and row["cleanup_confirmed"] is True
    assert row["result_ref"] == external["result_ref"]
    assert row["response_ref"] == outcome.trace.response_ref
    metadata = execution["history"].objects.get_json(row["metadata_ref"], ObjectType.BLOB)
    assert metadata["container_id"] and metadata["wall_ms"] > 0
    assert "container_id" not in saved.model_dump() and "wall_ms" not in saved.model_dump()
    assert (
        execution["history"].branch(execution["repository"])["head_commit_id"]
        == execution["checkpoint"]
    )


@pytest.mark.parametrize("mode", ["snippet", "script"])
def test_python_sources_stdin_argv_and_cwd(execution, mode):
    code = (
        b"import sys,json,pathlib; data=json.load(sys.stdin); "
        b"pathlib.Path('result').write_text(str(sum(data))+sys.argv[1])"
    )
    manifest, refs = execution["service"].authorize_inputs(
        execution["repository"],
        execution["run"],
        execution["checkpoint"],
        {"work/calc.py": code},
        blobs=(code, b"[2,3]"),
    )
    options = {"code_ref": refs[0]} if mode == "snippet" else {"script_path": "calc.py"}
    arguments = CodeArguments(
        input_manifest_ref=manifest,
        execution_policy_ref=execution["policy"].digest(),
        cwd="work",
        stdin_ref=refs[1],
        argv=("项",),
        **options,
    )
    outcome = call(execution, arguments, provider="tool:code_exec/v1")
    assert outcome.error is None, outcome.error
    saved = result(execution, outcome)
    manifest = parse_contract(
        execution["history"].objects.get(saved.output_manifest_ref, ObjectType.ARTIFACT_MANIFEST),
        ExecutionManifest,
    )
    file = next(item for item in manifest.files if item.path == "work/result")
    assert execution["history"].objects.get(file.object_ref, ObjectType.BLOB) == "5项".encode()


@pytest.mark.parametrize(
    ("program", "timeout", "error_code", "exit_code"),
    [
        (b"printf partial > result; exit 23", 30000, ErrorCode.EXECUTION_FAILED, 23),
        (b"exit 124", 30000, ErrorCode.EXECUTION_FAILED, 124),
        (b"ln -s /etc/passwd link", 30000, ErrorCode.EXPORT_INVALID, 0),
        (b"sleep 60 & wait", 2500, ErrorCode.EXECUTION_TIMEOUT, None),
    ],
)
def test_known_failures_do_not_publish_partial_workspace(
    execution, program, timeout, error_code, exit_code
):
    arguments = request(execution, program, timeout=timeout)
    outcome = call(execution, arguments)
    assert outcome.error.code == error_code
    saved = result(execution, outcome)
    assert saved.error_code == error_code.value and saved.output_manifest_ref is None
    assert saved.cleanup_confirmed is True
    if exit_code is not None:
        assert saved.exit_code == exit_code
    assert rows(execution, "external_calls")[0]["status"] == "failed"
    assert rows(execution, "sandbox_executions")[0]["status"] == "complete"
    assert (
        execution["history"].branch(execution["repository"])["head_commit_id"]
        == execution["checkpoint"]
    )


def test_intent_transaction_failure_rolls_back_all_execution_rows(execution):
    def fail(point):
        if point == "sandbox_intent_transaction":
            raise RuntimeError("执行意图事务故障注入")

    execution["service"].fault = fail
    with pytest.raises(RuntimeError, match="事务故障"):
        call(execution, request(execution))
    assert rows(execution, "external_calls") == []
    assert rows(execution, "sandbox_executions") == []


def test_foreign_object_reference_is_denied_before_reading_cas(execution, monkeypatch):
    arguments = request(execution)
    foreign = execution["history"].register_input(execution["repository"], b"foreign-canary")
    reads = []
    original = execution["history"].objects.get

    def read(object_id, object_type):
        reads.append(object_id)
        return original(object_id, object_type)

    monkeypatch.setattr(execution["history"].objects, "get", read)
    with pytest.raises(AgentGitError) as caught:
        call(execution, arguments.model_copy(update={"command_ref": foreign}))
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert foreign not in reads
    assert rows(execution, "sandbox_executions") == []


def test_completed_result_is_reused_without_accessing_docker(execution):
    arguments = request(execution)
    first = call(execution, arguments)

    def unavailable():
        pytest.fail("完整录制复用不能访问 Docker")

    execution["service"].engine_factory = unavailable
    second = call(execution, arguments)
    assert second == first
    assert len(rows(execution, "sandbox_executions")) == 1


def wait_for_execution(execution):
    deadline = monotonic() + 8
    while monotonic() < deadline:
        current = rows(execution, "sandbox_executions")
        if current and current[0]["container_id"]:
            try:
                top = execution["engine"].request(
                    "GET", f"/containers/{current[0]['container_id']}/top"
                )
                if any(row[0] == "10002" for row in top["Processes"]):
                    return current[0]
            except EngineError as error:
                assert error.status in (404, 409)
        sleep(0.05)
    pytest.fail("命令执行身份未出现")


def test_cancel_stops_actual_container_and_preserves_checkpoint(execution):
    arguments = request(execution, b"printf partial > result; sleep 30 & wait")
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(call, execution, arguments)
        wait_for_execution(execution)
        run = execution["queue"].run(execution["repository"], execution["run"])
        execution["queue"].request_control(
            execution["repository"],
            execution["run"],
            "cancel",
            expected_version=run["state_version"],
        )
        outcome = running.result(timeout=12)
    saved = result(execution, outcome)
    assert outcome.error.code == ErrorCode.EXECUTION_CANCELLED
    assert saved.cleanup_confirmed and saved.output_manifest_ref is None
    assert (
        execution["queue"].run(execution["repository"], execution["run"])["control_status"]
        == "requested"
    )
    assert (
        execution["history"].branch(execution["repository"])["head_commit_id"]
        == execution["checkpoint"]
    )


def test_expired_lease_kills_old_container_and_rejects_late_result(execution):
    arguments = request(execution, b"printf partial > result; sleep 30 & wait")
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(call, execution, arguments)
        old = wait_for_execution(execution)
        execution["heartbeat"].stop.set()
        execution["heartbeat"].thread.join(timeout=5)
        with execution["history"].engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == execution["lease"].job_id)
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        takeover = execution["queue"].claim("takeover-worker")
        assert takeover is not None and takeover.fencing_token > execution["lease"].fencing_token
        with pytest.raises(AgentGitError) as caught:
            running.result(timeout=12)
    assert caught.value.code == ErrorCode.VERSION_CONFLICT
    with pytest.raises(EngineError) as missing:
        execution["engine"].request("GET", f"/containers/{old['container_id']}/json")
    assert missing.value.status == 404
    assert rows(execution, "external_calls")[0]["result_ref"] is None
    assert (
        ExecutionService.reconcile(
            execution["queue"], takeover, engine_factory=lambda: execution["engine"]
        )
        == 1
    )
    assert rows(execution, "sandbox_executions")[0]["cleanup_confirmed"] is True
    assert rows(execution, "sandbox_executions")[0]["result_ref"] is None


def test_saved_result_survives_failure_before_checkpoint_without_restart(execution):
    arguments = request(execution)

    def crash():
        raise RuntimeError("结果已落库后的故障注入")

    with pytest.raises(RuntimeError, match="结果已落库"):
        call(execution, arguments, gateway=execution["gateway"](after_result=crash))
    assert rows(execution, "sandbox_executions")[0]["status"] == "complete"
    gateway = execution["gateway"]()
    gateway.managed = None
    outcome = call(execution, arguments, gateway=gateway)
    assert outcome.error is None
    assert len(rows(execution, "sandbox_executions")) == 1
    assert (
        execution["history"].branch(execution["repository"])["head_commit_id"]
        == execution["checkpoint"]
    )


def test_result_transaction_failure_does_not_publish_half_recording(execution):
    def fail(point):
        if point == "sandbox_result_transaction":
            raise RuntimeError("结果事务故障注入")

    execution["service"].fault = fail
    with pytest.raises(RuntimeError, match="结果事务"):
        call(execution, request(execution))
    row = rows(execution, "sandbox_executions")[0]
    assert row["result_ref"] is None and row["response_ref"] is None
    assert rows(execution, "external_calls")[0]["result_ref"] is None
    with pytest.raises(EngineError) as missing:
        execution["engine"].request("GET", f"/containers/{row['container_id']}/json")
    assert missing.value.status == 404


def test_lost_management_connection_never_claims_cleanup_or_success(execution):
    class Disconnected(DockerEngine):
        blocked = False

        def request(self, *args, **kwargs):
            if self.blocked:
                raise EngineError()
            return super().request(*args, **kwargs)

    engine = Disconnected(execution["engine"].endpoint)
    execution["service"].engine_factory = lambda: engine

    def fault(point):
        if point == "sandbox_after_execution":
            engine.blocked = True

    execution["service"].fault = fault
    try:
        outcome = call(execution, request(execution))
        saved = result(execution, outcome)
        assert outcome.error.code == ErrorCode.OUTCOME_UNKNOWN
        assert not saved.cleanup_confirmed and saved.output_manifest_ref is None
        assert rows(execution, "external_calls")[0]["result_ref"] is None
        row = rows(execution, "sandbox_executions")[0]
        assert row["status"] == "outcome_unknown" and not row["cleanup_confirmed"]
        with pytest.raises(AgentGitError) as caught:
            call(execution, request(execution))
        assert caught.value.code == ErrorCode.OUTCOME_UNKNOWN
    finally:
        engine.blocked = False
        from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer

        row = rows(execution, "sandbox_executions")[0]
        container = SandboxContainer(
            engine,
            row["image_id"],
            ExecutionIdentity(
                str(row["run_id"]), str(row["call_id"]), row["attempt"], row["fencing_token"]
            ),
            30000,
        )
        assert container.cleanup()


def test_missing_pinned_image_is_known_failure_without_execution(execution):
    policy = execution["policy"].model_copy(update={"image_id": "sha256:" + "f" * 64})
    execution["policy"] = policy
    execution["service"].policy = policy
    outcome = call(execution, request(execution))
    saved = result(execution, outcome)
    assert outcome.error.code == ErrorCode.SANDBOX_UNAVAILABLE
    assert saved.exit_code is None and saved.cleanup_confirmed
    assert rows(execution, "sandbox_executions")[0]["container_id"] is None


def test_unsupported_language_is_rejected_before_intent(execution):
    terminal = request(execution)
    arguments = CodeArguments(
        language="javascript",
        code_ref=terminal.command_ref,
        input_manifest_ref=terminal.input_manifest_ref,
        execution_policy_ref=terminal.execution_policy_ref,
    )
    with pytest.raises(AgentGitError) as caught:
        call(execution, arguments, provider="tool:code_exec/v1")
    assert caught.value.code == ErrorCode.UNSUPPORTED_LANGUAGE
    assert rows(execution, "sandbox_executions") == []


def test_completed_execution_cannot_be_changed_or_deleted(execution):
    assert call(execution, request(execution)).error is None
    table = TABLES["sandbox_executions"]
    for mutation in (table.update().values(cleanup_confirmed=False), table.delete()):
        with pytest.raises(sa.exc.DBAPIError), execution["history"].engine.begin() as connection:
            connection.execute(mutation)


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        (b"python -I -c 'data=bytearray(700*1024*1024)'", ErrorCode.RESOURCE_LIMIT),
        (
            b"python -I -c \"from pathlib import Path; Path('big').write_bytes(b'x'*9000000)\"",
            ErrorCode.OUTPUT_LIMIT,
        ),
    ],
)
def test_resource_and_file_limit_have_recorded_evidence(execution, program, expected):
    outcome = call(execution, request(execution, program))
    saved = result(execution, outcome)
    assert outcome.error.code == expected
    assert saved.error_code == expected.value and saved.output_manifest_ref is None
    assert saved.cleanup_confirmed
    row = rows(execution, "sandbox_executions")[0]
    metadata = execution["history"].objects.get_json(row["metadata_ref"], ObjectType.BLOB)
    if expected == ErrorCode.RESOURCE_LIMIT:
        assert (
            metadata["cgroup"]["memory.events:oom_kill"] > 0
            or metadata["pre_cleanup_state"]["OOMKilled"]
        )


def test_truncated_binary_logs_are_saved_as_bytes(execution):
    program = (
        b"python -I -c \"import os; os.write(1,b'\\xff'*2000000); os.write(2,b'\\xfe'*2000000)\""
    )
    outcome = call(execution, request(execution, program))
    assert outcome.error is None
    saved = result(execution, outcome)
    assert saved.stdout_truncated and saved.stderr_truncated
    assert execution["history"].objects.get(saved.stdout_ref, ObjectType.BLOB) == b"\xff" * 1048576
    assert execution["history"].objects.get(saved.stderr_ref, ObjectType.BLOB) == b"\xfe" * 1048576


def test_small_result_budget_is_rejected_before_start(execution):
    arguments = request(execution)
    gateway = execution["gateway"]()
    with pytest.raises(AgentGitError) as caught:
        gateway.call(
            step="execute",
            attempt=1,
            provider="tool:terminal_exec/v1",
            signature=execution["service"].signature("tool:terminal_exec/v1"),
            effect=EffectClass.NON_IDEMPOTENT_WRITE,
            request=encode(arguments.model_dump(mode="json")),
            invoke=lambda: pytest.fail("禁止回退"),
            max_bytes=128,
        )
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT
    assert rows(execution, "sandbox_executions") == []


def test_pid1_deadline_during_export_keeps_timeout_classification(execution, monkeypatch):
    from agentgit.adapters.execution.container import SandboxContainer

    export = SandboxContainer.export

    def delayed(container):
        # 故障注入让独立 PID 1 在采集调用前到期；不能用用户命令的退出码判定。
        sleep(3.2)
        return export(container)

    monkeypatch.setattr(SandboxContainer, "export", delayed)
    outcome = call(execution, request(execution, b"printf complete > result", timeout=3000))
    saved = result(execution, outcome)
    assert saved.exit_code == 0
    assert outcome.error.code == ErrorCode.EXECUTION_TIMEOUT
    assert saved.output_manifest_ref is None and saved.cleanup_confirmed


def test_real_process_death_is_reconciled_without_reexecuting(execution, tmp_path):
    arguments = request(execution, b"printf partial > result; sleep 30 & wait")
    with execution["history"].engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    lease = execution["lease"]
    data = {
        "lease": {
            "job_id": str(lease.job_id),
            "repository_id": str(lease.repository_id),
            "run_id": str(lease.run_id),
            "worker": lease.worker,
            "fencing_token": lease.fencing_token,
            "lease_ms": lease.lease_ms,
        },
        "arguments": arguments.model_dump(mode="json"),
        "policy": execution["policy"].model_dump(mode="json"),
    }
    input_path = tmp_path / "helper-input.json"
    input_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    helper = Path(__file__).resolve().parents[1] / "helpers/durable_sandbox_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            schema,
            str(execution["queue"].research.data_root),
            str(execution["history"].objects.root),
            str(input_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        old = wait_for_execution(execution)
        process.kill()
        process.wait(timeout=5)
        execution["heartbeat"].stop.set()
        execution["heartbeat"].thread.join(timeout=5)
        assert execution["engine"].request("GET", f"/containers/{old['container_id']}/json")[
            "State"
        ]["Running"]
        with execution["history"].engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == lease.job_id)
                .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
        takeover = execution["queue"].claim("process-takeover")
        assert takeover is not None
        assert (
            ExecutionService.reconcile(
                execution["queue"], takeover, engine_factory=lambda: execution["engine"]
            )
            == 1
        )
        with pytest.raises(EngineError) as missing:
            execution["engine"].request("GET", f"/containers/{old['container_id']}/json")
        assert missing.value.status == 404
        assert len(rows(execution, "external_calls")) == 1
        current = rows(execution, "sandbox_executions")[0]
        assert current["status"] == "outcome_unknown" and current["cleanup_confirmed"]
        assert current["result_ref"] is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stderr.close()
