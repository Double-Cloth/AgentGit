"""本机管理与独立 Worker 入口；机器调用输出稳定 JSON。"""

import json
import platform
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Annotated, Any
from uuid import UUID

import typer
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

from agentgit import __version__
from agentgit.adapters.execution.policy import bundled_policy
from agentgit.config import Settings
from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FreezeValidator, NewExperiment
from agentgit.core.management import (
    ControlRun,
    DecideApproval,
    NewAgent,
    NewRepository,
    NewTask,
    SubmitRun,
)
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, BudgetLimits, ResearchGoal
from agentgit.core.versions import NewBranch, parse_replay
from agentgit.diagnostics import sandbox_prerequisites
from agentgit.services.management import ManagementService
from agentgit.storage.database import probe_database
from agentgit.worker import Worker


class JsonGroup(TyperGroup):
    # 锁定版本的 Typer 使用内置 Click，须捕获同一实现的解析异常。
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        try:
            result = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
            if isinstance(result, int) and result:
                raise SystemExit(result)
            return result
        except ClickException as exc:
            emit(
                {
                    "error": {
                        "code": ErrorCode.VALIDATION.value,
                        "message": "命令参数无效，请使用 --help 查看格式",
                    }
                }
            )
            raise SystemExit(exc.exit_code) from None


app = typer.Typer(help="AgentGit 本地开发入口", no_args_is_help=True, cls=JsonGroup)


def emit(value: Any) -> None:
    typer.echo(json.dumps(value))


@contextmanager
def management(testing: bool) -> Iterator[ManagementService]:
    service = None
    try:
        service = ManagementService.from_settings(Settings.from_env(), testing=testing)
        yield service
    except AgentGitError as exc:
        emit({"error": exc.as_dict()})
        raise typer.Exit(1) from None
    except SQLAlchemyError:
        emit(
            {
                "error": {
                    "code": ErrorCode.DATABASE_UNAVAILABLE.value,
                    "message": "本地服务不可用，请检查数据库和数据目录",
                }
            }
        )
        raise typer.Exit(1) from None
    except ValidationError:
        emit({"error": {"code": ErrorCode.VALIDATION.value, "message": "命令参数不符合内容契约"}})
        raise typer.Exit(1) from None
    except OSError:
        emit({"error": {"code": ErrorCode.CONFIGURATION.value, "message": "文件或数据目录不可用"}})
        raise typer.Exit(1) from None
    except typer.Exit:
        raise
    except Exception:
        emit({"error": {"code": ErrorCode.INTERNAL_ERROR.value, "message": "本地命令处理失败"}})
        raise typer.Exit(1) from None
    finally:
        if service is not None:
            service.history.engine.dispose()


@app.command("init")
def initialize(name: str, key: str = typer.Option(...), testing: bool = False) -> None:
    """创建执行历史项目，要求可重试的幂等键。"""
    with management(testing) as service:
        emit(service.create_repository(NewRepository(name=name, expected_version=0), key))


@app.command("config")
def configure(
    repository_id: UUID,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    file: Path | None = None,
    with_execution: bool = False,
    testing: bool = False,
) -> None:
    """创建不可变配置版本，可从严格 JSON 文件读取。"""
    with management(testing) as service:
        if with_execution and file is not None:
            raise AgentGitError(ErrorCode.VALIDATION, "--with-execution 与 --file 不能同时使用")
        config = AgentConfig()
        if with_execution:
            config = AgentConfig(
                tools=(*config.tools, "code_exec", "terminal_exec"),
                execution_policy=bundled_policy(),
            )
        if file is not None:
            with file.open("rb") as stream:
                raw = stream.read(1_048_577)
            if len(raw) > 1_048_576:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "配置文件超过上限")
            config = parse_contract(raw, AgentConfig)
        emit(
            service.create_agent(
                repository_id, NewAgent(config=config, expected_version=expected_version), key
            )
        )


@app.command("task")
def task(
    repository_id: UUID,
    config_id: UUID,
    goal: str,
    config_version: int = typer.Option(..., min=1),
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """以固定配置创建研究任务。"""
    with management(testing) as service:
        payload = parse_contract(
            encode(
                {
                    "schema_version": 1,
                    "config_id": str(config_id),
                    "config_version": config_version,
                    "goal": ResearchGoal(text=goal).model_dump(mode="json"),
                    "expected_version": 0,
                }
            ),
            NewTask,
        )
        emit(service.create_task(repository_id, payload, key))


@app.command("freeze-validator")
def freeze_validator(
    repository_id: UUID,
    source_run: Annotated[UUID, typer.Option()],
    from_commit: Annotated[str, typer.Option()],
    key: Annotated[str, typer.Option()],
    fixture_split: str = "screen",
    testing: bool = False,
) -> None:
    """从明确归属的历史输入冻结固定验收规则，不执行 trial。"""
    with management(testing) as service:
        payload = parse_contract(
            encode(
                {
                    "schema_version": 1,
                    "source_run_id": str(source_run),
                    "from_commit": from_commit,
                    "fixture_split": fixture_split,
                }
            ),
            FreezeValidator,
        )
        emit(service.freeze_validator(repository_id, payload, key))


@app.command("experiment")
def experiment(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """按冻结 JSON 条件原子排队 A/B trial；执行交给独立 Worker。"""
    with management(testing) as service:
        with file.open("rb") as stream:
            payload = stream.read(1_048_577)
        if len(payload) > 1_048_576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "实验输入文件超过上限")
        emit(service.create_experiment(repository_id, parse_contract(payload, NewExperiment), key))


@app.command("experiment-status")
def experiment_status(repository_id: UUID, experiment_id: UUID, testing: bool = False) -> None:
    """读取实际 Job/Run、冻结验收和 trial 计量。"""
    with management(testing) as service:
        emit(service.experiment(repository_id, experiment_id))


@app.command("replay")
def replay(
    repository_id: UUID,
    source_run: Annotated[UUID, typer.Option()],
    from_commit: Annotated[str, typer.Option()],
    branch_name: Annotated[str, typer.Option()],
    key: Annotated[str, typer.Option()],
    until_commit: str | None = None,
    mode: str = "recorded",
    overrides_file: Path | None = None,
    budget_file: Path | None = None,
    testing: bool = False,
) -> None:
    """提交独立 Replay Job；Live 干预和预算使用严格 JSON 文件。"""
    with management(testing) as service:
        value: dict[str, Any] = {
            "schema_version": 1,
            "mode": mode,
            "source_run_id": str(source_run),
            "from_commit": from_commit,
            "branch_name": branch_name,
            "expected_version": 0,
        }
        if until_commit is not None:
            value["until_commit"] = until_commit
        if mode != "live" and (overrides_file is not None or budget_file is not None):
            raise AgentGitError(ErrorCode.VALIDATION, "只有 Live 可以提供干预或新预算文件")
        for field, path in (("overrides", overrides_file), ("budget", budget_file)):
            if path is not None:
                with path.open("rb") as stream:
                    raw = stream.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Replay 输入文件超过上限")
                value[field] = decode(raw)
        payload = parse_replay(encode(value))
        emit(service.replay(repository_id, payload, key))


@app.command("branch")
def branch(
    repository_id: UUID,
    name: str | None = None,
    source_run: UUID | None = None,
    from_commit: str | None = None,
    key: str | None = None,
    after_name: str | None = None,
    limit: int = typer.Option(100, min=1, max=1000),
    testing: bool = False,
) -> None:
    """无 name 时列出分支；创建时必须提供来源 Run、检查点和幂等键。"""
    with management(testing) as service:
        if name is None:
            if any(value is not None for value in (source_run, from_commit, key)):
                raise AgentGitError(ErrorCode.VALIDATION, "列出分支不能携带创建参数")
            emit(service.branches(repository_id, after_name=after_name, limit=limit))
            return
        if source_run is None or from_commit is None or key is None or after_name is not None:
            raise AgentGitError(ErrorCode.VALIDATION, "创建分支必须提供来源 Run、检查点和幂等键")
        payload = parse_contract(
            encode(
                {
                    "schema_version": 1,
                    "name": name,
                    "source_run_id": str(source_run),
                    "from_commit": from_commit,
                    "expected_version": 0,
                }
            ),
            NewBranch,
        )
        emit(service.create_branch(repository_id, payload, key))


@app.command("log")
def history_log(
    repository_id: UUID,
    branch_name: str = "main",
    after_commit: str | None = None,
    limit: int = typer.Option(100, min=1, max=1000),
    testing: bool = False,
) -> None:
    """从分支 HEAD 向父节点分页读取，不改变当前工作区或分支。"""
    with management(testing) as service:
        emit(
            service.commits(
                repository_id, branch_name=branch_name, after_commit=after_commit, limit=limit
            )
        )


@app.command("checkout")
def checkout(repository_id: UUID, commit_id: str, testing: bool = False) -> None:
    """只读检查 Commit、完整状态和产物引用，不执行历史命令。"""
    with management(testing) as service:
        emit(service.checkout(repository_id, commit_id))


@app.command("diff")
def structural_diff(
    repository_id: UUID,
    left_commit: str,
    right_commit: str,
    after: int = typer.Option(0, min=0),
    limit: int = typer.Option(100, min=1, max=1000),
    testing: bool = False,
) -> None:
    """分页显示 JSON Pointer、产物 hash 与调用差异及共同祖先。"""
    with management(testing) as service:
        emit(service.diff(repository_id, left_commit, right_commit, after=after, limit=limit))


@app.command("run")
def run(
    repository_id: UUID,
    task_id: UUID,
    config_version: int = typer.Option(..., min=1),
    expected_branch_version: int = typer.Option(..., min=0),
    expected_head: str | None = None,
    branch: str = "main",
    budget_file: Path | None = None,
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """耐久提交任务；本命令不执行 Agent。"""
    with management(testing) as service:
        budget = None
        if budget_file is not None:
            with budget_file.open("rb") as stream:
                raw = stream.read(1_048_577)
            if len(raw) > 1_048_576:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "预算文件超过上限")
            budget = parse_contract(raw, BudgetLimits).model_dump(mode="json")
        payload = parse_contract(
            encode(
                {
                    "schema_version": 1,
                    "config_version": config_version,
                    "expected_branch_version": expected_branch_version,
                    "expected_head": expected_head,
                    "branch_name": branch,
                    "budget": budget,
                }
            ),
            SubmitRun,
        )
        emit(service.submit(repository_id, task_id, payload, key))


@app.command("status")
def status(
    repository_id: UUID,
    entity_id: Annotated[UUID | None, typer.Argument()] = None,
    job: bool = False,
    testing: bool = False,
) -> None:
    """读取 Run 或 Job 的状态和预算。"""
    with management(testing) as service:
        if entity_id is None:
            if job:
                raise AgentGitError(ErrorCode.VALIDATION, "查询 Job 需要提供 Job ID")
            emit(service.repository(repository_id))
            return
        emit(
            service.job(repository_id, entity_id) if job else service.run(repository_id, entity_id)
        )


def control(
    action: str, repository_id: UUID, run_id: UUID, expected_version: int, key: str, testing: bool
) -> None:
    with management(testing) as service:
        emit(
            service.control(
                repository_id, run_id, action, ControlRun(expected_version=expected_version), key
            )
        )


@app.command("pause")
def pause(
    repository_id: UUID,
    run_id: UUID,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """请求在安全点暂停。"""
    control("pause", repository_id, run_id, expected_version, key, testing)


@app.command("resume")
def resume(
    repository_id: UUID,
    run_id: UUID,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """请求在同一 Run 恢复普通暂停。"""
    control("resume", repository_id, run_id, expected_version, key, testing)


@app.command("cancel")
def cancel(
    repository_id: UUID,
    run_id: UUID,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """记录取消请求，不宣称撤销外部动作。"""
    control("cancel", repository_id, run_id, expected_version, key, testing)


@app.command("artifacts")
def artifacts(repository_id: UUID, run_id: UUID, testing: bool = False) -> None:
    """列出当前检查点的产物。"""
    with management(testing) as service:
        emit(service.artifacts(repository_id, run_id))


@app.command("approvals")
def approvals(
    repository_id: UUID,
    run_id: UUID,
    after_seq: int = typer.Option(0, min=0),
    limit: int = typer.Option(100, min=1, max=1000),
    testing: bool = False,
) -> None:
    """分页读取动作原文、摘要和审批状态。"""
    with management(testing) as service:
        emit(service.approvals(repository_id, run_id, after_seq, limit))


@app.command("decision")
def decision(
    repository_id: UUID,
    approval_id: UUID,
    decision: str,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """对精确动作提交 approve 或 reject。"""
    with management(testing) as service:
        payload = parse_contract(
            encode(
                {"schema_version": 1, "decision": decision, "expected_version": expected_version}
            ),
            DecideApproval,
        )
        emit(service.decide(repository_id, approval_id, payload, key))


@app.command("worker")
def worker(
    once: bool = False, poll_ms: int = typer.Option(1000, min=100, max=60000), testing: bool = False
) -> None:
    """独立领取耐久 Job；空队列时有界等待。"""
    with management(testing) as service:
        runner = Worker(service.jobs)
        try:
            while True:
                result = runner.run_once()
                if result is not None or once:
                    emit(result or {"status": "idle"})
                if once:
                    if result is not None and result["status"] in (
                        "failed",
                        "lease_lost",
                        "outcome_unknown",
                        "budget_exhausted",
                    ):
                        raise typer.Exit(1)
                    return
                if result is None:
                    Event().wait(poll_ms / 1000)
        except KeyboardInterrupt:
            emit({"status": "worker_stopped"})


@app.command("serve")
def serve(port: int = typer.Option(8000, min=1, max=65535)) -> None:
    """以回环地址启动本地 API，需要预先配置身份令牌。"""
    try:
        settings = Settings.from_env()
        if settings.api_token is None:
            raise AgentGitError(ErrorCode.CONFIGURATION, "请先设置 AGENTGIT_API_TOKEN")
        import uvicorn

        uvicorn.run("agentgit.api.main:create_app", factory=True, host="127.0.0.1", port=port)
    except AgentGitError as exc:
        emit({"error": exc.as_dict()})
        raise typer.Exit(1) from None


@app.command()
def version() -> None:
    """显示程序及 Python 版本。"""
    typer.echo(json.dumps({"agentgit": __version__, "python": platform.python_version()}))


@app.command()
def doctor(database: bool = False, sandbox: bool = False, testing: bool = False) -> None:
    """按需探测数据库与执行容器前提。"""
    report: dict[str, Any] = {"agentgit": __version__, "python": platform.python_version()}
    try:
        settings = Settings.from_env()
        if testing:
            settings.assert_test_isolation()
        if database:
            report["postgresql"] = probe_database(settings, testing=testing)
        if sandbox:
            report["sandbox"] = sandbox_prerequisites()
        # JSON 使用 ASCII 转义，避免 Windows 重定向时受本地代码页影响。
        typer.echo(json.dumps(report))
        if sandbox and not report["sandbox"]["execution_ready"]:
            raise typer.Exit(1)
    except AgentGitError as exc:
        typer.echo(json.dumps({"error": exc.as_dict()}))
        raise typer.Exit(1) from None
