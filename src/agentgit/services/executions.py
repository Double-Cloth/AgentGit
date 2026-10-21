"""租约保护的固定容器执行；输入/结果引用与调用事务绑定。"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, RowMapping

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine, EngineError, ExecOutput
from agentgit.adapters.execution.protocol import validate_files
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import (
    SANDBOX_PROVIDERS,
    CodeArguments,
    ExecutionArguments,
    ExecutionFile,
    ExecutionManifest,
    ExecutionPolicy,
    ExecutionResult,
    TerminalArguments,
)
from agentgit.core.jobs import Lease
from agentgit.core.models import EffectClass, parse_contract
from agentgit.core.snapshots import EventPayload
from agentgit.services.history import TABLES, EventInput, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.managed_calls import ManagedCallContext, ManagedCallResult
from agentgit.services.recovery import recovery_origins
from agentgit.storage.objects import StoredObject


@dataclass(frozen=True)
class _Plan:
    arguments: ExecutionArguments
    files: dict[str, bytes]
    command: list[str]
    stdin: bytes
    references: frozenset[str]
    policy_object: StoredObject


@dataclass(frozen=True)
class _Completed:
    result: ExecutionResult
    objects: tuple[StoredObject, ...]
    metadata: StoredObject


class _Abort(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code


class ExecutionService:
    @staticmethod
    def reconcile(
        queue: JobService,
        lease: Lease,
        *,
        engine_factory: Callable[[], DockerEngine] = DockerEngine.local,
    ) -> int:
        """接管时先核对旧容器；清理确认只关闭物理执行，不伪造丢失结果。"""
        history, table = queue.history, TABLES["sandbox_executions"]
        origins = recovery_origins(queue, lease)
        with history.engine.begin() as connection:
            history._run(connection, lease.repository_id, lease.run_id, lock=True)
            queue.guard(connection, lease, lease.run_id)
            pending = list(
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == lease.repository_id,
                        table.c.run_id.in_((lease.run_id, *origins)),
                        table.c.job_id == lease.job_id,
                        table.c.fencing_token < lease.fencing_token,
                        table.c.status != "complete",
                        table.c.cleanup_confirmed.is_(False),
                    )
                ).mappings()
            )
        count = 0
        for row in pending:
            try:
                container = SandboxContainer(
                    engine_factory(),
                    row["image_id"],
                    ExecutionIdentity(
                        str(row["run_id"]),
                        str(row["call_id"]),
                        row["attempt"],
                        row["fencing_token"],
                    ),
                    row["effective_timeout_ms"],
                )
                container.container_id = row["container_id"]
                if not container.cleanup():
                    continue
            except AgentGitError:
                continue
            evidence = history.objects.put_json(
                ObjectType.BLOB,
                {
                    "schema_version": 1,
                    "container_name": row["container_name"],
                    "container_id": row["container_id"],
                    "cleanup_confirmed": True,
                    "previous_fencing_token": row["fencing_token"],
                    "observer_fencing_token": lease.fencing_token,
                },
            )
            event = EventInput(
                "sandbox.cleanup_confirmed",
                "recovery",
                {
                    "call_id": str(row["call_id"]),
                    "evidence_ref": evidence.id,
                },
            )
            payload = history.objects.put_json(
                ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
            )
            with history.engine.begin() as connection:
                run = history._run(connection, lease.repository_id, lease.run_id, lock=True)
                queue.guard(connection, lease, lease.run_id)
                current = (
                    connection.execute(
                        sa.select(table).where(table.c.call_id == row["call_id"]).with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if current["status"] == "complete" or current["cleanup_confirmed"]:
                    continue
                history._ref(
                    connection,
                    lease.repository_id,
                    evidence,
                    "sandbox_execution",
                    str(row["call_id"]),
                    "recovery_cleanup",
                )
                connection.execute(
                    table.update()
                    .where(table.c.call_id == row["call_id"])
                    .values(
                        cleanup_confirmed=True,
                        status="outcome_unknown",
                    )
                )
                seq = int(run["last_event_seq"]) + 1
                history._event(connection, lease.repository_id, lease.run_id, seq, event, payload)
                connection.execute(
                    TABLES["runs"]
                    .update()
                    .where(TABLES["runs"].c.id == lease.run_id)
                    .values(last_event_seq=seq)
                )
                count += 1
        return count

    def __init__(
        self,
        history: HistoryService,
        queue: JobService,
        lease: Lease,
        policy: ExecutionPolicy,
        *,
        engine_factory: Callable[[], DockerEngine] = DockerEngine.local,
        fault: Callable[[str], None] | None = None,
    ):
        self.history, self.queue, self.lease, self.policy = history, queue, lease, policy
        self.engine_factory = engine_factory
        self.fault = fault or (lambda _: None)
        self._plans: dict[UUID, _Plan] = {}
        self._completed: dict[UUID, _Completed] = {}

    def signature(self, provider: str) -> str:
        if provider not in SANDBOX_PROVIDERS:
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行服务未登记该工具")
        arguments = TerminalArguments if provider == "tool:terminal_exec/v1" else CodeArguments
        return object_digest(
            ObjectType.CALL,
            encode(
                {
                    "schema_version": 1,
                    "provider": provider,
                    "version": "v1",
                    "effect": EffectClass.NON_IDEMPOTENT_WRITE.value,
                    "scope": "sandbox",
                    "policy_ref": self.policy.digest(),
                    "arguments": arguments.model_json_schema(),
                }
            ),
        )

    def authorize_inputs(
        self,
        repository_id: UUID,
        run_id: UUID,
        checkpoint: str,
        files: dict[str, bytes],
        *,
        blobs: tuple[bytes, ...] = (),
    ) -> tuple[str, tuple[str, ...]]:
        """仅供后端从当前工作区/已授权任务输入建立清单，不接受外部 CAS 猜测引用。"""
        validate_files(files)
        if len(blobs) > 3 or sum(map(len, blobs)) > 8388608 + 65536:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "额外执行输入超过固定限制")
        entries = []
        objects = []
        for path, payload in sorted(files.items()):
            item = self.history.objects.put(ObjectType.BLOB, payload)
            objects.append(item)
            entries.append(
                ExecutionFile(
                    path=path,
                    object_ref=item.id,
                    media_type=item.media_type,
                    byte_length=len(payload),
                )
            )
        extra = tuple(self.history.objects.put(ObjectType.BLOB, payload) for payload in blobs)
        manifest = ExecutionManifest(files=tuple(entries))
        saved = self.history.objects.put_json(
            ObjectType.ARTIFACT_MANIFEST, manifest.model_dump(mode="json")
        )
        with self.history.engine.begin() as connection:
            run = self.history._run(connection, repository_id, run_id, lock=True)
            self.history.assert_writer(connection, repository_id, run_id)
            self._guard(connection, repository_id, run_id)
            if run["checkpoint_commit_id"] != checkpoint:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "执行输入的检查点已改变")
            for item in (*objects, *extra, saved):
                self.history._ref(
                    connection, repository_id, item, "sandbox_input", str(run_id), checkpoint
                )
        return saved.id, tuple(item.id for item in extra)

    def stage(self, context: ManagedCallContext) -> None:
        if context.max_bytes < self.policy.result_envelope_bytes:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "响应预算不足以保存完整执行结果封装")
        if context.checkpoint is None or context.occurrence.signature_digest != self.signature(
            context.occurrence.provider
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行缺少安全检查点或固定工具签名")
        request: ExecutionArguments
        if context.occurrence.provider == "tool:terminal_exec/v1":
            terminal = parse_contract(context.request, TerminalArguments)
            request = terminal
            references = {terminal.command_ref}
        else:
            code = parse_contract(context.request, CodeArguments)
            request = code
            if code.language != "python":
                raise AgentGitError(ErrorCode.UNSUPPORTED_LANGUAGE, "首版仅支持 Python 3.12")
            references = set()
            if code.code_ref is not None:
                references.add(code.code_ref)
        if request.execution_policy_ref != self.policy.digest():
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行策略引用不匹配")
        references.add(request.input_manifest_ref)
        if request.stdin_ref is not None:
            references.add(request.stdin_ref)
        self._check_input_scope(context, references)
        if isinstance(request, TerminalArguments):
            program = self.history.objects.get(request.command_ref, ObjectType.BLOB)
            if len(program) > 16384:
                raise AgentGitError(ErrorCode.VALIDATION, "终端命令超过 16 KiB")
            command = [self.policy.shell, "-c", self._text(program)]
        elif isinstance(request, CodeArguments) and request.code_ref is not None:
            program = self.history.objects.get(request.code_ref, ObjectType.BLOB)
            if len(program) > 65536:
                raise AgentGitError(ErrorCode.VALIDATION, "代码片段超过 64 KiB")
            command = [self.policy.interpreter, "-I", "-c", self._text(program), *request.argv]
        else:
            assert isinstance(request, CodeArguments) and request.script_path is not None
            command = [self.policy.interpreter, "-I", request.script_path, *request.argv]
        manifest = parse_contract(
            self.history.objects.get(request.input_manifest_ref, ObjectType.ARTIFACT_MANIFEST),
            ExecutionManifest,
        )
        references.update(file.object_ref for file in manifest.files)
        self._check_input_scope(context, references)
        files = {}
        for file in manifest.files:
            payload = self.history.objects.get(file.object_ref, ObjectType.BLOB)
            if len(payload) != file.byte_length:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "执行输入文件大小与清单不符")
            files[file.path] = payload
            references.add(file.object_ref)
        if request.cwd != "." and not any(path.startswith(request.cwd + "/") for path in files):
            raise AgentGitError(ErrorCode.VALIDATION, "执行 cwd 不存在于输入工作区")
        if isinstance(request, CodeArguments) and request.script_path is not None:
            path = (
                request.script_path
                if request.cwd == "."
                else f"{request.cwd}/{request.script_path}"
            )
            if path not in files:
                raise AgentGitError(ErrorCode.VALIDATION, "执行脚本不存在于输入工作区")
        stdin = b""
        if request.stdin_ref is not None:
            stdin = self.history.objects.get(request.stdin_ref, ObjectType.BLOB)
            references.add(request.stdin_ref)
        references.add(request.input_manifest_ref)
        policy = self.history.objects.put(
            ObjectType.BLOB, encode(self.policy.model_dump(mode="json"))
        )
        # 同一 Job 串行执行，只保留当前调用，避免按调用数累积完整输入字节。
        self._plans = {
            context.call_id: _Plan(request, files, command, stdin, frozenset(references), policy)
        }

    @staticmethod
    def _text(payload: bytes) -> str:
        try:
            value = payload.decode("utf-8")
            if "\0" in value:
                raise ValueError
            return value
        except (UnicodeDecodeError, ValueError):
            raise AgentGitError(
                ErrorCode.VALIDATION, "执行命令/代码必须是无 NUL 的 UTF-8"
            ) from None

    def _guard(self, connection: Connection, repository_id: UUID, run_id: UUID) -> RowMapping:
        if repository_id != self.lease.repository_id or run_id != self.lease.run_id:
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行服务不属于当前租约")
        return self.queue.guard(connection, self.lease, run_id)

    def _check_input_scope(self, context: ManagedCallContext, references: set[str]) -> None:
        with self.history.engine.begin() as connection:
            run = self.history._run(connection, context.repository_id, context.run_id, lock=True)
            self.history.assert_writer(connection, context.repository_id, context.run_id)
            self._guard(connection, context.repository_id, context.run_id)
            if run["checkpoint_commit_id"] != context.checkpoint:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "执行输入的检查点已改变")
            self._assert_scope(connection, context, references)

    @staticmethod
    def _assert_scope(
        connection: Connection, context: ManagedCallContext, references: set[str] | frozenset[str]
    ) -> None:
        refs = TABLES["object_refs"]
        authorized = set(
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == context.repository_id,
                    refs.c.owner_type == "sandbox_input",
                    refs.c.owner_id == str(context.run_id),
                    refs.c.role == context.checkpoint,
                    refs.c.object_id.in_(references),
                )
            ).scalars()
        )
        if authorized != references:
            raise AgentGitError(ErrorCode.FORBIDDEN, "执行输入未在当前 Run/检查点授权")

    def prepare(self, connection: Connection, context: ManagedCallContext) -> None:
        plan = self._plans[context.call_id]
        job = self._guard(connection, context.repository_id, context.run_id)
        self._assert_scope(connection, context, plan.references)
        now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        remaining = int((job["deadline_at"] - now).total_seconds() * 1000)
        effective = min(plan.arguments.timeout_ms, self.policy.max_timeout_ms, remaining)
        if effective <= 0:
            raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "Run 已无容器执行时间预算")
        identity = self._identity(context)
        self.history._ref(
            connection,
            context.repository_id,
            plan.policy_object,
            "sandbox_execution",
            str(context.call_id),
            "policy",
        )
        connection.execute(
            TABLES["sandbox_executions"]
            .insert()
            .values(
                call_id=context.call_id,
                repository_id=context.repository_id,
                run_id=context.run_id,
                job_id=self.lease.job_id,
                checkpoint_commit_id=context.checkpoint,
                request_digest=context.occurrence.request_digest,
                policy_ref=plan.policy_object.id,
                input_manifest_ref=plan.arguments.input_manifest_ref,
                image_id=self.policy.image_id,
                container_name=identity.name,
                attempt=context.occurrence.attempt,
                fencing_token=self.lease.fencing_token,
                effective_timeout_ms=effective,
            )
        )
        self.fault("sandbox_intent_transaction")

    def _identity(self, context: ManagedCallContext) -> ExecutionIdentity:
        return ExecutionIdentity(
            str(context.run_id),
            str(context.call_id),
            context.occurrence.attempt,
            self.lease.fencing_token,
        )

    def _control(self, context: ManagedCallContext) -> None:
        with self.history.engine.begin() as connection:
            run = self.history._run(connection, context.repository_id, context.run_id, lock=True)
            self.history.assert_writer(connection, context.repository_id, context.run_id)
            job = self._guard(connection, context.repository_id, context.run_id)
            if run["checkpoint_commit_id"] != context.checkpoint:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "执行中的检查点已改变")
            if run["control_status"] == "requested" and run["control_request"] == "cancel":
                raise _Abort(ErrorCode.EXECUTION_CANCELLED)
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            if job["deadline_at"] <= now:
                raise _Abort(ErrorCode.EXECUTION_TIMEOUT)

    def invoke(self, context: ManagedCallContext) -> ManagedCallResult:
        plan = self._plans[context.call_id]
        table = TABLES["sandbox_executions"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(table.c.call_id == context.call_id))
                .mappings()
                .one()
            )
        start = monotonic()
        container: SandboxContainer | None = None
        output: ExecOutput | None = None
        counters: dict[str, Any] = {}
        state_proof: dict[str, Any] = {}
        files: dict[str, bytes] | None = None
        failure: ErrorCode | None = None
        lease_error: AgentGitError | None = None
        cleanup = False
        try:
            self._control(context)
            self.fault("sandbox_before_create")
            engine = self.engine_factory()
            container = SandboxContainer(
                engine, self.policy.image_id, self._identity(context), row["effective_timeout_ms"]
            )
            container.create()
            with self.history.engine.begin() as connection:
                self.history._run(connection, context.repository_id, context.run_id, lock=True)
                self.history.assert_writer(connection, context.repository_id, context.run_id)
                self._guard(connection, context.repository_id, context.run_id)
                connection.execute(
                    table.update()
                    .where(table.c.call_id == context.call_id)
                    .values(container_id=container.container_id, status="running")
                )
            self.fault("sandbox_after_create")
            self._control(context)
            container.start()
            self.fault("sandbox_after_start")
            counters = container.ready()["counters"]
            container.load(plan.files)
            self._control(context)
            box: list[ExecOutput | Exception] = []

            def execute() -> None:
                try:
                    assert container is not None
                    box.append(
                        container.execute(plan.command, stdin=plan.stdin, cwd=plan.arguments.cwd)
                    )
                except Exception as error:
                    box.append(error)

            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            while thread.is_alive():
                thread.join(timeout=0.1)
                try:
                    self._control(context)
                    if monotonic() - start >= row["effective_timeout_ms"] / 1000:
                        raise _Abort(ErrorCode.EXECUTION_TIMEOUT)
                except (_Abort, AgentGitError):
                    container.stop()
                    thread.join(timeout=5)
                    if box and isinstance(box[0], ExecOutput):
                        output = box[0]
                    raise
            if not box or isinstance(box[0], Exception):
                raise EngineError()
            output = box[0]
            self.fault("sandbox_after_execution")
            state = container.inspect()["State"]
            if not state["Running"]:
                failure = (
                    ErrorCode.RESOURCE_LIMIT
                    if state["OOMKilled"]
                    else ErrorCode.EXECUTION_TIMEOUT
                    if state["ExitCode"] == 124
                    else ErrorCode.EXPORT_INVALID
                )
            else:
                after = container.ready()["counters"]
                if (
                    after["memory.events:oom_kill"] > counters["memory.events:oom_kill"]
                    or after["pids.events:max"] > counters["pids.events:max"]
                    or (
                        output.exit_code != 0
                        and min(after["workspace.free_bytes"], after["tmp.free_bytes"]) == 0
                    )
                ):
                    failure = ErrorCode.RESOURCE_LIMIT
                counters = after
                if failure is None and output.exit_code == 0:
                    try:
                        files, receipt = container.export()
                        counters = receipt["counters"]
                    except AgentGitError as error:
                        failure = (
                            ErrorCode.OUTPUT_LIMIT
                            if error.code == ErrorCode.OUTPUT_LIMIT
                            else ErrorCode.EXPORT_INVALID
                        )
                elif failure is None:
                    failure = ErrorCode.EXECUTION_FAILED
        except _Abort as error:
            failure = error.code
        except AgentGitError as error:
            if error.code == ErrorCode.VERSION_CONFLICT:
                lease_error = error
            failure = ErrorCode.SANDBOX_UNAVAILABLE
        except Exception:
            failure = ErrorCode.SANDBOX_UNAVAILABLE
        finally:
            if container is not None and container.container_id is not None:
                try:
                    state = container.inspect()["State"]
                    state_proof = {key: state[key] for key in ("Running", "OOMKilled", "ExitCode")}
                    if not state["Running"] and failure in (
                        None,
                        ErrorCode.SANDBOX_UNAVAILABLE,
                        ErrorCode.EXPORT_INVALID,
                    ):
                        if state["OOMKilled"]:
                            failure = ErrorCode.RESOURCE_LIMIT
                        elif state["ExitCode"] == 124:
                            # 此处是可信 PID 1 的状态，不是用户命令自报的 124。
                            failure = ErrorCode.EXECUTION_TIMEOUT
                except EngineError:
                    pass
            if container is None or not container.creation_attempted:
                cleanup = True
            else:
                cleanup = container.cleanup()
        if lease_error is not None:
            raise lease_error
        if not cleanup:
            failure = ErrorCode.OUTCOME_UNKNOWN
        if output is None or output.exit_code is None:
            failure = failure or ErrorCode.OUTCOME_UNKNOWN
        if failure is not None:
            files = None
        return self._save_bytes(
            context,
            plan,
            output,
            files,
            failure,
            cleanup,
            counters,
            state_proof,
            int((monotonic() - start) * 1000),
            container,
        )

    def _save_bytes(
        self,
        context: ManagedCallContext,
        plan: _Plan,
        output: ExecOutput | None,
        files: dict[str, bytes] | None,
        failure: ErrorCode | None,
        cleanup: bool,
        counters: dict[str, Any],
        state_proof: dict[str, Any],
        wall_ms: int,
        container: SandboxContainer | None,
    ) -> ManagedCallResult:
        objects: list[StoredObject] = []
        stdout = self.history.objects.put(ObjectType.BLOB, output.stdout if output else b"")
        stderr = self.history.objects.put(ObjectType.BLOB, output.stderr if output else b"")
        objects.extend((stdout, stderr))
        manifest_ref = None
        if files is not None:
            entries = []
            for path, content in sorted(files.items()):
                item = self.history.objects.put(ObjectType.BLOB, content)
                objects.append(item)
                entries.append(
                    ExecutionFile(
                        path=path,
                        object_ref=item.id,
                        byte_length=len(content),
                        media_type=item.media_type,
                    )
                )
            manifest = self.history.objects.put_json(
                ObjectType.ARTIFACT_MANIFEST,
                ExecutionManifest(files=tuple(entries)).model_dump(mode="json"),
            )
            objects.append(manifest)
            manifest_ref = manifest.id
        arguments = plan.arguments
        result = ExecutionResult(
            exit_code=output.exit_code if output else None,
            error_code=cast(Any, failure.value if failure else None),
            stdout_ref=stdout.id,
            stderr_ref=stderr.id,
            stdout_truncated=output.stdout_truncated if output else True,
            stderr_truncated=output.stderr_truncated if output else True,
            input_manifest_ref=arguments.input_manifest_ref,
            output_manifest_ref=manifest_ref,
            command_ref=arguments.command_ref if isinstance(arguments, TerminalArguments) else None,
            code_ref=arguments.code_ref if isinstance(arguments, CodeArguments) else None,
            script_path=arguments.script_path if isinstance(arguments, CodeArguments) else None,
            image_id=self.policy.image_id,
            interpreter=self.policy.shell
            if isinstance(arguments, TerminalArguments)
            else self.policy.interpreter,
            interpreter_version=self.policy.shell_version
            if isinstance(arguments, TerminalArguments)
            else self.policy.interpreter_version,
            execution_policy_ref=self.policy.digest(),
            cleanup_confirmed=cleanup,
            integrity_complete=files is not None and failure is None,
        )
        metadata = self.history.objects.put_json(
            ObjectType.BLOB,
            {
                "schema_version": 1,
                "container_name": self._identity(context).name,
                "container_id": container.container_id if container else None,
                "wall_ms": wall_ms,
                "cgroup": counters,
                "pre_cleanup_state": state_proof,
                "cleanup_confirmed": cleanup,
            },
        )
        self._completed = {context.call_id: _Completed(result, tuple(objects), metadata)}
        return ManagedCallResult(
            encode(result.model_dump(mode="json")),
            AgentGitError(failure, "容器执行未成功，详见录制结果") if failure else None,
        )

    def record(
        self,
        connection: Connection,
        context: ManagedCallContext,
        response: StoredObject,
        result: StoredObject,
    ) -> None:
        saved = self._completed.get(context.call_id)
        if saved is None:
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "执行缺少完整采集与清理证据")
        if response.id != object_digest(
            ObjectType.BLOB, encode(saved.result.model_dump(mode="json"))
        ):
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "执行结果封装被截断或与采集结果不符")
        self._guard(connection, context.repository_id, context.run_id)
        for item in (*saved.objects, saved.metadata):
            self.history._ref(
                connection,
                context.repository_id,
                item,
                "sandbox_execution",
                str(context.call_id),
                "evidence",
            )
        unknown = saved.result.error_code == "outcome_unknown"
        connection.execute(
            TABLES["sandbox_executions"]
            .update()
            .where(
                TABLES["sandbox_executions"].c.call_id == context.call_id,
                TABLES["sandbox_executions"].c.fencing_token == self.lease.fencing_token,
            )
            .values(
                status="outcome_unknown" if unknown else "complete",
                cleanup_confirmed=saved.result.cleanup_confirmed,
                response_ref=response.id,
                result_ref=None if unknown else result.id,
                metadata_ref=saved.metadata.id,
                finished_at=sa.func.clock_timestamp(),
            )
        )
        self.fault("sandbox_result_transaction")
