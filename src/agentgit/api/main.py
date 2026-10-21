"""本机业务入口：只提交耐久 Job，执行交给独立 Worker。"""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException

from agentgit import __version__
from agentgit.api.security import LocalBoundary, RequestStats, error_response
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
from agentgit.core.versions import NewBranch, parse_replay
from agentgit.services.management import ManagementService
from agentgit.storage.database import probe_database

Repository = Annotated[UUID, Header(alias="X-Repository-ID")]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def create_app(
    settings: Settings | None = None, *, management: ManagementService | None = None
) -> FastAPI:
    config = settings or Settings.from_env()
    if management is not None and management.actor != config.operator_id:
        raise AgentGitError(ErrorCode.CONFIGURATION, "API 与管理服务的操作者不一致")
    manager, owns_manager, lock = management, management is None, Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_manager and manager is not None:
            manager.history.engine.dispose()

    app = FastAPI(title="AgentGit", version=__version__, lifespan=lifespan)
    logger = logging.getLogger("agentgit.http")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    stats = RequestStats()
    app.add_middleware(LocalBoundary, settings=config, stats=stats)

    def get_manager() -> ManagementService:
        nonlocal manager
        with lock:
            if manager is None:
                manager = ManagementService.from_settings(config)
        return manager

    @app.exception_handler(AgentGitError)
    async def domain_error(request: Request, exc: AgentGitError) -> JSONResponse:
        return error_response(exc.code, exc.message, request.state.correlation_id)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            ErrorCode.VALIDATION, "请求字段、类型或分页参数无效", request.state.correlation_id
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        if isinstance(exc, IntegrityError):
            return error_response(
                ErrorCode.VERSION_CONFLICT, "请求违反归属或版本约束", request.state.correlation_id
            )
        return error_response(
            ErrorCode.DATABASE_UNAVAILABLE, "数据库服务暂不可用", request.state.correlation_id
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.VALIDATION
        return error_response(
            code, "请求不可访问或不被支持", request.state.correlation_id, exc.status_code
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(
            ErrorCode.INTERNAL_ERROR,
            "请求处理失败，请使用关联编号核查",
            request.state.correlation_id,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    def metrics(
        repo: Repository, service: Annotated[ManagementService, Depends(get_manager)]
    ) -> dict[str, Any]:
        return {"http": stats.snapshot(), **service.metrics(repo)}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        if manager is None:
            probe_database(config)
        else:
            with manager.history.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.post("/repositories", status_code=201)
    def repository(
        body: dict[str, Any],
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_repository(parse_contract(encode(body), NewRepository), key)

    @app.get("/repositories/{repository_id}")
    def repository_info(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        if repository_id != repo:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        return service.repository(repo)

    @app.post("/agents", status_code=201)
    def agent(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_agent(repo, parse_contract(encode(body), NewAgent), key)

    @app.post("/branches", status_code=201)
    def branch(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_branch(repo, parse_contract(encode(body), NewBranch), key)

    @app.post("/replays", status_code=202)
    def replay(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.replay(repo, parse_replay(encode(body)), key)

    @app.get("/replays/{job_id}")
    def replay_status(
        job_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.replay_status(repo, job_id)

    @app.get("/repositories/{repository_id}/branches")
    def branches(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_name: Annotated[str | None, Query(max_length=100)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        if repo != repository_id:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        return service.branches(repo, after_name=after_name, limit=limit)

    @app.get("/repositories/{repository_id}/commits")
    def commits(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        branch_name: Annotated[str, Query(min_length=1, max_length=100)] = "main",
        after_commit: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        if repo != repository_id:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        return service.commits(
            repo, branch_name=branch_name, after_commit=after_commit, limit=limit
        )

    @app.get("/commits/{commit_id}")
    def commit(
        commit_id: str,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.checkout(repo, commit_id)

    @app.get("/diff")
    def diff(
        left_commit: str,
        right_commit: str,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        return service.diff(repo, left_commit, right_commit, after=after, limit=limit)

    @app.post("/tasks", status_code=201)
    def task(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_task(repo, parse_contract(encode(body), NewTask), key)

    @app.post("/tasks/{task_id}/runs", status_code=202)
    def submit(
        task_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.submit(repo, task_id, parse_contract(encode(body), SubmitRun), key)

    @app.get("/runs/{run_id}")
    def run(
        run_id: UUID, repo: Repository, service: Annotated[ManagementService, Depends(get_manager)]
    ) -> dict[str, Any]:
        return service.run(repo, run_id)

    @app.get("/jobs/{job_id}")
    def job(
        job_id: UUID, repo: Repository, service: Annotated[ManagementService, Depends(get_manager)]
    ) -> dict[str, Any]:
        return service.job(repo, job_id)

    @app.post("/runs/{run_id}/{action}", status_code=202)
    def control(
        run_id: UUID,
        action: Literal["pause", "resume", "cancel"],
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.control(repo, run_id, action, parse_contract(encode(body), ControlRun), key)

    @app.get("/runs/{run_id}/events")
    def events(
        run_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        return service.events(repo, run_id, after_seq, limit)

    @app.get("/runs/{run_id}/artifacts")
    def artifacts(
        run_id: UUID, repo: Repository, service: Annotated[ManagementService, Depends(get_manager)]
    ) -> dict[str, Any]:
        return service.artifacts(repo, run_id)

    @app.get("/artifacts/{artifact_id}/content")
    def artifact(
        artifact_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> Response:
        row, content = service.artifact(repo, artifact_id)
        media_type = (
            row["media_type"]
            if re.fullmatch(r"[\w.+-]+/[\w.+-]+", row["media_type"], flags=re.ASCII)
            else "application/octet-stream"
        )
        return Response(
            content,
            media_type=media_type,
            headers={
                "ETag": f'"{row["object_id"]}"',
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(row["relative_path"].rsplit("/", 1)[-1], safe=""),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/approvals")
    def approvals(
        run_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        return service.approvals(repo, run_id, after_seq, limit)

    @app.post("/approvals/{approval_id}/decision", status_code=202)
    def decision(
        approval_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.decide(repo, approval_id, parse_contract(encode(body), DecideApproval), key)

    return app
