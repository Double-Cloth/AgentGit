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
from agentgit.adapters.mcp import configured_client
from agentgit.config import Settings
from agentgit.core.canonical import decode, encode
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FreezeValidator, NewExperiment
from agentgit.core.interventions import NewIntervention
from agentgit.core.langgraph import LangGraphProgram
from agentgit.core.langgraph_recorded import NewLangGraphRecorded
from agentgit.core.langgraph_runs import NewLangGraphFork
from agentgit.core.management import (
    ControlRun,
    DecideApproval,
    NewAgent,
    NewRepository,
    NewTask,
    SubmitRun,
)
from agentgit.core.mcp import MCPServer, MCPStdioServer
from agentgit.core.mcp_runs import MCPProgram, MCPRecordedSpec
from agentgit.core.merge_proposals import (
    ApproveMergeProposal,
    NewMergeProposal,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)
from agentgit.core.merge_runs import ForkMergedBranch, NewMergedRun
from agentgit.core.models import Contract, EffectClass, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.repair_pools import NewPreparedRepairSearch, NewRepairValidationSuite
from agentgit.core.repair_reviews import DecideRepairReview
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import AgentConfig, BudgetLimits, ResearchGoal
from agentgit.core.versions import NewBranch, parse_replay
from agentgit.diagnostics import sandbox_prerequisites
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.langgraph_runs import LangGraphRunService
from agentgit.services.management import ManagementService
from agentgit.services.mcp_runs import MCPRunService
from agentgit.services.versions import VersionService
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


def _mcp_input[T: Contract](file: Path, model: type[T], max_bytes: int = 2_097_152) -> T:
    with file.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "适配器输入文件超过上限")
    return parse_contract(payload, model)


@app.command("mcp-bind")
def mcp_bind(
    server_identity: str,
    endpoint: str,
    tool_name: str,
    effect: Annotated[EffectClass, typer.Option()],
    timeout_ms: int = typer.Option(2000, min=20, max=30000),
    max_response_bytes: int = typer.Option(65536, min=128, max=1048576),
    testing: bool = False,
) -> None:
    """操作者明确登记可信回环工具，发现定义并固定本地效果，不调用工具。"""
    with management(testing):
        server = MCPServer(
            server_identity=server_identity,
            endpoint=endpoint,
            timeout_ms=timeout_ms,
            max_response_bytes=max_response_bytes,
        )
        emit(configured_client(server).bind(tool_name, effect).model_dump(mode="json"))


@app.command("mcp-submit")
def mcp_submit(
    repository_id: UUID,
    file: Path,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    branch: str = "main",
    head: str | None = None,
    replay_file: Path | None = None,
    testing: bool = False,
) -> None:
    """提交固定单次 MCP 程序；需要审批的动作由现有 Worker 停放等待。"""
    with management(testing) as service:
        program = _mcp_input(file, MCPProgram)
        replay = _mcp_input(replay_file, MCPRecordedSpec, 4096) if replay_file else None
        submitted = MCPRunService(service.jobs).submit(
            repository_id,
            program,
            actor=service.actor,
            key=key,
            branch_name=branch,
            expected_head=head,
            expected_version=expected_version,
            replay=replay,
        )
        emit(submitted.model_dump(mode="json"))


@app.command("langgraph-submit")
def langgraph_submit(
    repository_id: UUID,
    file: Path,
    expected_version: int = typer.Option(..., min=0),
    key: str = typer.Option(...),
    branch: str = "main",
    head: str | None = None,
    testing: bool = False,
) -> None:
    """提交固定只读取证图；通过现有 Worker/状态与控制命令执行和恢复。"""
    with management(testing) as service:
        program = _mcp_input(file, LangGraphProgram)
        submitted = LangGraphRunService(service.jobs).submit(
            repository_id,
            program,
            actor=service.actor,
            key=key,
            branch_name=branch,
            expected_head=head,
            expected_version=expected_version,
        )
        emit(submitted.model_dump(mode="json"))


@app.command("langgraph-recorded")
def langgraph_recorded(
    repository_id: UUID, file: Path, key: str = typer.Option(...), testing: bool = False
) -> None:
    """固定来源范围并提交只消费录制的独立图任务。"""
    with management(testing) as service:
        request = _mcp_input(file, NewLangGraphRecorded, 16384)
        result = LangGraphRunService(service.jobs).recorded(
            repository_id, request, actor=service.actor, key=key
        )
        emit(result.model_dump(mode="json"))


@app.command("langgraph-fork")
def langgraph_fork(
    repository_id: UUID, file: Path, key: str = typer.Option(...), testing: bool = False
) -> None:
    """从完整 review 检查点创建独立图任务，只编辑审阅备注。"""
    with management(testing) as service:
        request = _mcp_input(file, NewLangGraphFork, 16384)
        emit(
            LangGraphRunService(service.jobs)
            .fork(repository_id, request, actor=service.actor, key=key)
            .model_dump(mode="json")
        )


@app.command("mcp-stdio-bind")
def mcp_stdio_bind(
    server_identity: str,
    program_ref: str,
    tool_name: str,
    effect: Annotated[EffectClass, typer.Option()],
    timeout_ms: int = typer.Option(2000, min=20, max=30000),
    max_response_bytes: int = typer.Option(65536, min=128, max=1048576),
    testing: bool = False,
) -> None:
    """仅从后端批准白名单发现工具；不接受命令、参数或凭据。"""
    with management(testing):
        server = MCPStdioServer(
            server_identity=server_identity,
            program_ref=program_ref,
            timeout_ms=timeout_ms,
            max_response_bytes=max_response_bytes,
        )
        emit(configured_client(server).bind(tool_name, effect).model_dump(mode="json"))


@app.command("mcp-fork")
def mcp_fork(repository_id: UUID, file: Path, testing: bool = False) -> None:
    """从明确的 MCP 安全检查点建立来源分支，不启动任务或继承审批。"""
    with management(testing) as service:
        request = _mcp_input(file, NewBranch, 4096)
        emit(VersionService(service.history).fork(repository_id, request, actor=service.actor))


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


@app.command("intervention-context")
def intervention_context(
    repository_id: UUID,
    source_run: Annotated[UUID, typer.Option()],
    from_commit: Annotated[str, typer.Option()],
    validator_ref: Annotated[str, typer.Option()],
    target_event_id: Annotated[int, typer.Option(min=1)],
    testing: bool = False,
) -> None:
    """读取源事件、检查点和原配置，生成可核对的干预前置条件。"""
    with management(testing) as service:
        emit(
            service.intervention_context(
                repository_id, source_run, from_commit, target_event_id, validator_ref
            )
        )


@app.command("intervention")
def intervention(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """验证目标事件与前置条件，创建不可变干预；不执行试验。"""
    with management(testing) as service:
        with file.open("rb") as stream:
            payload = stream.read(1_048_577)
        if len(payload) > 1_048_576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "干预文件超过上限")
        emit(
            service.create_intervention(
                repository_id, parse_contract(payload, NewIntervention), key
            )
        )


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


@app.command("repair-validation-suite")
def repair_validation_suite(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """在筛选首次领取前预注册六次确认条件与四个固定回归任务。"""
    with management(testing) as service:
        with file.open("rb") as stream:
            payload = stream.read(1_048_577)
        if len(payload) > 1_048_576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "确认池输入文件超过上限")
        emit(
            service.create_repair_validation_suite(
                repository_id, parse_contract(payload, NewRepairValidationSuite), key
            )
        )


@app.command("repair-validation-open")
def repair_validation_open(
    repository_id: UUID,
    study_id: UUID,
    suite_id: UUID,
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """最终候选锁后一次性开启固定池，不执行模型或工具。"""
    with management(testing) as service:
        emit(service.open_repair_validation(repository_id, study_id, suite_id, key))


@app.command("repair-validation-status")
def repair_validation_status(repository_id: UUID, study_id: UUID, testing: bool = False) -> None:
    """读取冻结计划；未执行的采样票据不能当作通过证据。"""
    with management(testing) as service:
        emit(service.repair_validation(repository_id, study_id))


@app.command("repair-confirm-worker")
def repair_confirm_worker(
    once: bool = False,
    repository_id: UUID | None = None,
    poll_ms: int = typer.Option(1000, min=100, max=60000),
    testing: bool = False,
) -> None:
    """推进已开启池的确认与回归；普通 worker 执行实际研究 Job。"""
    with management(testing) as service:
        runner, previous = RepairConfirmationWorker(service.research), None
        try:
            while True:
                result = runner.run_once(repository=repository_id)
                summary: dict[str, Any] = (
                    {
                        key: result[key]
                        for key in ("study_id", "status", "stop_reason", "usage", "verification")
                    }
                    if result
                    else {"status": "idle"}
                )
                progress = {
                    **summary,
                    "usage": {
                        key: value
                        for key, value in summary.get("usage", {}).items()
                        if key != "elapsed_wall_ms"
                    },
                }
                if once or (result is not None and progress != previous):
                    emit(summary)
                previous = progress
                if once:
                    return
                Event().wait(poll_ms / 1000)
        except KeyboardInterrupt:
            emit({"status": "repair_confirm_worker_stopped"})


@app.command("repair-reduce")
def repair_reduce(
    repository_id: UUID,
    search_id: UUID,
    key: Annotated[str, typer.Option()],
    max_trials: int | None = typer.Option(None, min=0, max=100),
    testing: bool = False,
) -> None:
    """从已封存的待确认筛选建立唯一后续缩减；沿用原搜索预算。"""
    with management(testing) as service:
        emit(
            service.create_repair_study(
                repository_id,
                NewRepairStudy(search_id=str(search_id), max_reduce_trials=max_trials),
                key,
            )
        )


@app.command("repair-propose")
def repair_propose(
    repository_id: UUID, study_id: UUID, key: Annotated[str, typer.Option()], testing: bool = False
) -> None:
    """从封存确认生成唯一精确提案，不批准或执行。"""
    with management(testing) as service:
        emit(service.create_repair_proposal(repository_id, study_id, key))


def _merge_request[T: Contract](file: Path, model: type[T]) -> T:
    with file.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "合并请求超过 1 MiB")
    return parse_contract(raw, model)


@app.command("merge-fork")
def merge_fork(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """从精确批准建立独立分支，不创建 Run 或继承授权。"""
    request = _merge_request(file, ForkMergedBranch)
    with management(testing) as service:
        emit(service.fork_merged_branch(repository_id, request, key))


@app.command("merge-run")
def merge_run(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """显式选择配置、验收器和预算，提交独立合并分支的新任务。"""
    request = _merge_request(file, NewMergedRun)
    with management(testing) as service:
        emit(service.start_merged_run(repository_id, request, key))


@app.command("merge-propose")
def merge_propose(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """固定三方来源，建立耐久合并候选；不更新 HEAD。"""
    request = _merge_request(file, NewMergeProposal)
    with management(testing) as service:
        emit(service.create_merge_proposal(repository_id, request, key))


@app.command("merge-status")
def merge_status(
    repository_id: UUID,
    proposal_id: UUID,
    testing: bool = False,
    review_before: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    """只读查询候选各版本、硬验证和拒绝决定。"""
    with management(testing) as service:
        emit(service.merge_proposal(repository_id, proposal_id, review_before=review_before))


@app.command("merge-review-status")
def merge_review_status(
    repository_id: UUID, proposal_id: UUID, review_id: UUID, testing: bool = False
) -> None:
    """按同项目精确票据 ID 只读查询，包括已过时审阅。"""
    with management(testing) as service:
        emit(service.merge_review_status(repository_id, proposal_id, review_id))


@app.command("merge-edit")
def merge_edit(
    repository_id: UUID,
    proposal_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """追加完整编辑定义；旧验证和审阅票据失效。"""
    request = _merge_request(file, ReplaceMergeRevision)
    with management(testing) as service:
        emit(service.replace_merge_revision(repository_id, proposal_id, request, key))


@app.command("merge-validate")
def merge_validate(
    repository_id: UUID,
    proposal_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """从固定来源重新计算当前修订并保存硬验证。"""
    request = _merge_request(file, ValidateMergeRevision)
    with management(testing) as service:
        emit(service.validate_merge_revision(repository_id, proposal_id, request, key))


@app.command("merge-review")
def merge_review(
    repository_id: UUID,
    proposal_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """重查来源和版本后准备确切审阅票据；不批准或提交。"""
    request = _merge_request(file, PrepareMergeReview)
    with management(testing) as service:
        emit(service.begin_merge_review(repository_id, proposal_id, request, key))


@app.command("merge-approve")
def merge_approve(
    repository_id: UUID,
    proposal_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """批准本人已审阅的精确候选，原子创建双父 Commit 并推进目标 HEAD。"""
    request = _merge_request(file, ApproveMergeProposal)
    with management(testing) as service:
        emit(service.approve_merge_proposal(repository_id, proposal_id, request, key))


@app.command("merge-reject")
def merge_reject(
    repository_id: UUID,
    proposal_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """记录对当前确切修订的拒绝；不更新分支。"""
    request = _merge_request(file, RejectMergeProposal)
    with management(testing) as service:
        emit(service.reject_merge_proposal(repository_id, proposal_id, request, key))


@app.command("repair-proposal-status")
def repair_proposal_status(repository_id: UUID, proposal_id: UUID, testing: bool = False) -> None:
    """读取精确前后配置、证据、范围与审阅决定。"""
    with management(testing) as service:
        emit(service.repair_proposal(repository_id, proposal_id))


@app.command("repair-review")
def repair_review(
    repository_id: UUID,
    proposal_id: UUID,
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """冻结当前配置版本的审阅票据；刷新不改提案内容。"""
    with management(testing) as service:
        emit(service.begin_repair_review(repository_id, proposal_id, key))


@app.command("repair-review-decision")
def repair_review_decision(
    repository_id: UUID,
    proposal_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """按明确票据批准、拒绝或保存实验；不启动 Run/Job。"""
    with file.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "审阅请求超过上限")
    with management(testing) as service:
        emit(
            service.decide_repair_review(
                repository_id, proposal_id, parse_contract(raw, DecideRepairReview), key
            )
        )


@app.command("repair-study-status")
def repair_study_status(repository_id: UUID, study_id: UUID, testing: bool = False) -> None:
    """读取共享预算、真实删除试验和最终候选锁。"""
    with management(testing) as service:
        emit(service.repair_study(repository_id, study_id))


@app.command("repair-study-cancel")
def repair_study_cancel(
    repository_id: UUID, study_id: UUID, key: Annotated[str, typer.Option()], testing: bool = False
) -> None:
    """停止新删除，向已派发研究 Job 传递取消。"""
    with management(testing) as service:
        emit(service.cancel_repair_study(repository_id, study_id, key))


@app.command("repair-study-worker")
def repair_study_worker(
    once: bool = False,
    repository_id: UUID | None = None,
    poll_ms: int = typer.Option(1000, min=100, max=60000),
    testing: bool = False,
) -> None:
    """独立推进耐久缩减；普通 worker 执行实际研究任务。"""
    with management(testing) as service:
        runner, previous = RepairStudyWorker(service.research), None
        try:
            while True:
                result = runner.run_once(repository=repository_id)
                summary: dict[str, Any] = (
                    {k: result[k] for k in ("study_id", "status", "stop_reason", "usage")}
                    if result
                    else {"status": "idle"}
                )
                progress = {
                    **summary,
                    "usage": {
                        k: v for k, v in summary.get("usage", {}).items() if k != "elapsed_wall_ms"
                    },
                }
                if once or (result is not None and progress != previous):
                    emit(summary)
                previous = progress
                if once:
                    return
                Event().wait(poll_ms / 1000)
        except KeyboardInterrupt:
            emit({"status": "repair_study_worker_stopped"})


@app.command("repair-search")
def repair_search(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """生成有限模板并排队搜索；不要求手工编写补丁 payload。"""
    with management(testing) as service:
        with file.open("rb") as stream:
            payload = stream.read(1_048_577)
        if len(payload) > 1_048_576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "修复搜索输入文件超过上限")
        emit(
            service.create_repair_search(
                repository_id, parse_contract(payload, NewRepairSearch), key
            )
        )


@app.command("repair-prepare")
def repair_prepare(
    repository_id: UUID,
    file: Annotated[Path, typer.Option()],
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """同一事务发布搜索及其确认/回归预注册，避免先行筛选。"""
    with management(testing) as service:
        with file.open("rb") as stream:
            payload = stream.read(1_048_577)
        if len(payload) > 1_048_576:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "修复预注册输入文件超过上限")
        emit(
            service.create_prepared_repair_search(
                repository_id, parse_contract(payload, NewPreparedRepairSearch), key
            )
        )


@app.command("repair-status")
def repair_status(repository_id: UUID, search_id: UUID, testing: bool = False) -> None:
    """读取冻结空间、独立试验、预算与停止报告。"""
    with management(testing) as service:
        emit(service.repair_search(repository_id, search_id))


@app.command("repair-cancel")
def repair_cancel(
    repository_id: UUID,
    search_id: UUID,
    key: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """停止扩展新候选，并向已派发研究 Job 传递取消。"""
    with management(testing) as service:
        emit(service.cancel_repair_search(repository_id, search_id, key))


@app.command("repair-list")
def repair_list(
    repository_id: UUID,
    after_id: UUID | None = None,
    limit: int = typer.Option(30, min=1, max=100),
    testing: bool = False,
) -> None:
    """发现项目内的持久搜索，不依赖先前进程内状态。"""
    with management(testing) as service:
        emit(service.repair_searches(repository_id, after_id=after_id, limit=limit))


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


@app.command("behavioral-trace")
def behavioral_trace(repository_id: UUID, run_id: UUID, testing: bool = False) -> None:
    """只读核验当前 Run 的事件、观测、计划依赖与证据缺口。"""
    with management(testing) as service:
        emit(service.behavioral_trace(repository_id, run_id))


@app.command("behavioral-diff")
def behavioral_diff(
    repository_id: UUID, left_run_id: UUID, right_run_id: UUID, testing: bool = False
) -> None:
    """按逻辑身份与已核验依赖对齐两个 Run，不执行干预或判断根因。"""
    with management(testing) as service:
        emit(service.behavioral_diff(repository_id, left_run_id, right_run_id))


@app.command("debug-report")
def create_debug_report(
    repository_id: UUID,
    failed_run_id: UUID,
    reference_run_id: UUID | None = None,
    key: str = typer.Option(...),
    testing: bool = False,
) -> None:
    """冻结失败硬断言、完整静态候选及证据，不执行干预。"""
    with management(testing) as service:
        request = NewDebugReport(
            failed_run_id=str(failed_run_id),
            reference_run_id=str(reference_run_id) if reference_run_id else None,
        )
        emit(service.create_debug_report(repository_id, request, key))


@app.command("debug-report-status")
def debug_report_status(repository_id: UUID, report_id: UUID, testing: bool = False) -> None:
    """读取已冻结的历史报告，不按当前规则重新定位或重新执行。"""
    with management(testing) as service:
        emit(service.debug_report(repository_id, report_id))


@app.command("debug-candidate-context")
def debug_candidate_context(
    repository_id: UUID,
    report_id: UUID,
    candidate_event_id: Annotated[int, typer.Option(min=1)],
    validator_ref: Annotated[str, typer.Option()],
    testing: bool = False,
) -> None:
    """只读解析候选对应的模型调用、安全检查点和 S7 前置条件。"""
    with management(testing) as service:
        emit(
            service.debug_candidate_context(
                repository_id, report_id, candidate_event_id, validator_ref
            )
        )


@app.command("debug-candidate-experiment")
def debug_candidate_experiment(
    repository_id: UUID,
    report_id: UUID,
    candidate_event_id: Annotated[int, typer.Option(min=1)],
    experiment_id: Annotated[UUID, typer.Option()],
    testing: bool = False,
) -> None:
    """核验既有单变量实验与候选的关系，展示真实 trial 和未知项。"""
    with management(testing) as service:
        emit(
            service.debug_candidate_experiment(
                repository_id, report_id, candidate_event_id, experiment_id
            )
        )


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


@app.command("repair-worker")
def repair_worker(
    once: bool = False,
    repository_id: UUID | None = None,
    poll_ms: int = typer.Option(1000, min=100, max=60000),
    testing: bool = False,
) -> None:
    """独立推进修复搜索；研究 Job 另由 worker 执行。"""
    with management(testing) as service:
        runner = RepairSearchWorker(service.research)
        previous = None
        try:
            while True:
                result = runner.run_once(repository=repository_id)
                summary: dict[str, Any] = (
                    {key: result[key] for key in ("search_id", "status", "stop_reason", "usage")}
                    if result is not None
                    else {"status": "idle"}
                )
                progress = {
                    **summary,
                    "usage": {
                        key: value
                        for key, value in summary.get("usage", {}).items()
                        if key != "elapsed_wall_ms"
                    },
                }
                if once or (result is not None and progress != previous):
                    emit(summary)
                previous = progress
                if once:
                    return
                Event().wait(poll_ms / 1000)
        except KeyboardInterrupt:
            emit({"status": "repair_worker_stopped"})


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
