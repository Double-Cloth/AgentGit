"""S1 开发入口，JSON 输出不包含配置秘密。"""

import json
import platform
from typing import Any

import typer

from agentgit import __version__
from agentgit.config import Settings
from agentgit.core.errors import AgentGitError
from agentgit.diagnostics import sandbox_prerequisites
from agentgit.storage.database import probe_database

app = typer.Typer(help="AgentGit 本地开发入口", no_args_is_help=True)


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
        typer.echo(json.dumps(report, ensure_ascii=False))
        if sandbox and not report["sandbox"]["execution_ready"]:
            raise typer.Exit(1)
    except AgentGitError as exc:
        typer.echo(json.dumps({"error": exc.as_dict()}, ensure_ascii=False))
        raise typer.Exit(1) from None
