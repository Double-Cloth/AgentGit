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
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
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
    testing: bool = False,
) -> None:
    """创建不可变配置版本，可从严格 JSON 文件读取。"""
    with management(testing) as service:
        config = AgentConfig()
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
