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
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FreezeValidator, NewExperiment
from agentgit.core.interventions import NewIntervention
from agentgit.core.management import (
    ControlRun,
    DecideApproval,
    NewAgent,
    NewRepository,
    NewTask,
    SubmitRun,
)
from agentgit.core.merge_proposals import (
    ApproveMergeProposal,
    NewMergeProposal,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)
from agentgit.core.merge_runs import ForkMergedBranch, NewMergedRun
from agentgit.core.models import Contract, parse_contract
from agentgit.core.repair import NewRepairSearch
from agentgit.core.repair_pools import (
    NewPreparedRepairSearch,
    NewRepairValidationSuite,
    OpenRepairValidationPool,
)
from agentgit.core.repair_reviews import DecideRepairReview
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.versions import NewBranch, parse_replay
from agentgit.services.execution_views import ExecutionResource
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

    @app.get("/capabilities")
    def capabilities(service: Annotated[ManagementService, Depends(get_manager)]) -> dict[str, Any]:
        return service.capabilities()

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

    @app.get("/repositories/{repository_id}/runs")
    def runs(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        if repository_id != repo:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        return service.runs(repo, after_id=after_id, limit=limit)

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

    @app.get("/intervention-context")
    def intervention_context(
        source_run_id: UUID,
        from_commit: str,
        validator_ref: str,
        target_event_id: Annotated[int, Query(ge=1)],
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.intervention_context(
            repo, source_run_id, from_commit, target_event_id, validator_ref
        )

    @app.post("/interventions", status_code=201)
    def intervention(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_intervention(repo, parse_contract(encode(body), NewIntervention), key)

    @app.get("/interventions/{intervention_id}")
    def intervention_view(
        intervention_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.intervention(repo, intervention_id)

    @app.post("/validators", status_code=201)
    def freeze_validator(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.freeze_validator(repo, parse_contract(encode(body), FreezeValidator), key)

    @app.get("/validators/{validator_ref}")
    def validator(
        validator_ref: str,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.validator(repo, validator_ref)

    @app.post("/repair-validation-suites", status_code=201)
    def repair_suite_create(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_repair_validation_suite(
            repo, parse_contract(encode(body), NewRepairValidationSuite), key
        )

    @app.get("/repair-validation-suites/{suite_id}")
    def repair_suite_status(
        suite_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.repair_validation_suite(repo, suite_id)

    @app.post("/repair-studies/{study_id}/open-validation")
    def repair_validation_open(
        study_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        request = parse_contract(encode(body), OpenRepairValidationPool)
        return service.open_repair_validation(repo, study_id, UUID(request.suite_id), key)

    @app.get("/repair-studies/{study_id}/validation")
    def repair_validation_status(
        study_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.repair_validation(repo, study_id)

    @app.post("/repair-studies/{study_id}/proposal", status_code=201)
    def repair_proposal_create(
        study_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        parse_contract(encode(body), Contract)
        return service.create_repair_proposal(repo, study_id, key)

    @app.get("/repair-proposals/{proposal_id}")
    def repair_proposal_status(
        proposal_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.repair_proposal(repo, proposal_id)

    @app.post("/repair-proposals/{proposal_id}/review", status_code=201)
    def repair_review_begin(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        parse_contract(encode(body), Contract)
        return service.begin_repair_review(repo, proposal_id, key)

    @app.post("/repair-proposals/{proposal_id}/approve")
    def repair_review_approve(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        request = parse_contract(encode(body), DecideRepairReview)
        if request.decision == "reject":
            raise AgentGitError(ErrorCode.VALIDATION, "拒绝决定必须使用 reject 入口")
        return service.decide_repair_review(repo, proposal_id, request, key)

    @app.post("/repair-proposals/{proposal_id}/reject")
    def repair_review_reject(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        request = parse_contract(encode(body), DecideRepairReview)
        if request.decision != "reject":
            raise AgentGitError(ErrorCode.VALIDATION, "采纳或保存实验必须使用 approve 入口")
        return service.decide_repair_review(repo, proposal_id, request, key)

    @app.post("/merge-proposals", status_code=201)
    def merge_proposal_create(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_merge_proposal(
            repo, parse_contract(encode(body), NewMergeProposal), key
        )

    @app.post("/merge-branches", status_code=201)
    def merged_branch_create(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.fork_merged_branch(repo, parse_contract(encode(body), ForkMergedBranch), key)

    @app.post("/merge-runs", status_code=202)
    def merged_run_create(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.start_merged_run(repo, parse_contract(encode(body), NewMergedRun), key)

    @app.get("/merge-proposals/{proposal_id}")
    def merge_proposal_status(
        proposal_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        review_before: UUID | None = None,
    ) -> dict[str, Any]:
        return service.merge_proposal(repo, proposal_id, review_before=review_before)

    @app.get("/merge-proposals/{proposal_id}/reviews/{review_id}")
    def merge_review_status(
        proposal_id: UUID,
        review_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.merge_review_status(repo, proposal_id, review_id)

    @app.post("/merge-proposals/{proposal_id}/revisions", status_code=201)
    def merge_revision_replace(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.replace_merge_revision(
            repo, proposal_id, parse_contract(encode(body), ReplaceMergeRevision), key
        )

    @app.post("/merge-proposals/{proposal_id}/validate", status_code=201)
    def merge_revision_validate(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.validate_merge_revision(
            repo, proposal_id, parse_contract(encode(body), ValidateMergeRevision), key
        )

    @app.post("/merge-proposals/{proposal_id}/review", status_code=201)
    def merge_review_prepare(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.begin_merge_review(
            repo, proposal_id, parse_contract(encode(body), PrepareMergeReview), key
        )

    @app.post("/merge-proposals/{proposal_id}/approve", status_code=201)
    def merge_proposal_approve(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.approve_merge_proposal(
            repo, proposal_id, parse_contract(encode(body), ApproveMergeProposal), key
        )

    @app.post("/merge-proposals/{proposal_id}/reject", status_code=201)
    def merge_proposal_reject(
        proposal_id: UUID,
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.reject_merge_proposal(
            repo, proposal_id, parse_contract(encode(body), RejectMergeProposal), key
        )

    @app.post("/repair-studies", status_code=202)
    def repair_study_create(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_repair_study(repo, parse_contract(encode(body), NewRepairStudy), key)

    @app.get("/repair-studies/{study_id}")
    def repair_study_status(
        study_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.repair_study(repo, study_id)

    @app.post("/repair-studies/{study_id}/cancel")
    def repair_study_cancel(
        study_id: UUID,
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.cancel_repair_study(repo, study_id, key)

    @app.post("/repair-searches/prepared", status_code=202)
    def repair_search_prepared(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_prepared_repair_search(
            repo, parse_contract(encode(body), NewPreparedRepairSearch), key
        )

    @app.post("/repair-searches", status_code=202)
    def repair_search_create(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_repair_search(
            repo, parse_contract(encode(body), NewRepairSearch), key
        )

    @app.get("/repair-searches/{search_id}/workflow")
    def repair_workflow_status(
        search_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.repair_workflow(repo, search_id)

    @app.get("/repair-searches/{search_id}")
    def repair_search_status(
        search_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.repair_search(repo, search_id)

    @app.post("/repair-searches/{search_id}/cancel")
    def repair_search_cancel(
        search_id: UUID,
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.cancel_repair_search(repo, search_id, key)

    @app.get("/repositories/{repository_id}/repair-searches")
    def repair_searches_list(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        if repo != repository_id:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        return service.repair_searches(repo, after_id=after_id, limit=limit)

    @app.post("/experiments", status_code=202)
    def experiment(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_experiment(repo, parse_contract(encode(body), NewExperiment), key)

    @app.get("/experiments/{experiment_id}")
    def experiment_status(
        experiment_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.experiment(repo, experiment_id)

    @app.get("/repositories/{repository_id}/experiments")
    def experiments_list(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        if repository_id != repo:
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        return service.experiments_list(repo, after_id=after_id, limit=limit)

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

    @app.get("/commits/{commit_id}/artifacts")
    def commit_artifacts(
        commit_id: str,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        return service.commit_artifacts(repo, commit_id, after_id=after_id, limit=limit)

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

    @app.get("/runs/{run_id}/behavioral-trace")
    def behavioral_trace(
        run_id: UUID, repo: Repository, service: Annotated[ManagementService, Depends(get_manager)]
    ) -> dict[str, Any]:
        return service.behavioral_trace(repo, run_id)

    @app.get("/behavioral-diff")
    def behavioral_diff(
        left_run_id: UUID,
        right_run_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.behavioral_diff(repo, left_run_id, right_run_id)

    @app.post("/debug-reports", status_code=201)
    def create_debug_report(
        body: dict[str, Any],
        repo: Repository,
        key: IdempotencyKey,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.create_debug_report(repo, parse_contract(encode(body), NewDebugReport), key)

    @app.get("/debug-reports/{report_id}")
    def debug_report(
        report_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.debug_report(repo, report_id)

    @app.get("/debug-reports/{report_id}/candidate-context")
    def debug_candidate_context(
        report_id: UUID,
        validator_ref: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$")],
        candidate_event_id: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)],
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.debug_candidate_context(repo, report_id, candidate_event_id, validator_ref)

    @app.get("/debug-reports/{report_id}/candidate-experiment")
    def debug_candidate_experiment(
        report_id: UUID,
        experiment_id: UUID,
        candidate_event_id: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)],
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.debug_candidate_experiment(
            repo, report_id, candidate_event_id, experiment_id
        )

    @app.get("/repositories/{repository_id}/debug-reports")
    def debug_reports_list(
        repository_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        if repository_id != repo:
            raise AgentGitError(ErrorCode.NOT_FOUND, "报告项目不可访问")
        return service.debug_reports_list(repo, after_id=after_id, limit=limit)

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

    @app.get("/runs/{run_id}/executions")
    def executions(
        run_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        after_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        return service.executions(repo, run_id, after_id=after_id, limit=limit)

    @app.get("/runs/{run_id}/executions/{call_id}")
    def execution(
        run_id: UUID,
        call_id: UUID,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
    ) -> dict[str, Any]:
        return service.execution(repo, run_id, call_id)

    @app.get("/runs/{run_id}/executions/{call_id}/content/{resource}")
    def execution_content(
        run_id: UUID,
        call_id: UUID,
        resource: ExecutionResource,
        repo: Repository,
        service: Annotated[ManagementService, Depends(get_manager)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_048_576)] = 65536,
    ) -> Response:
        metadata, content = service.execution_content(
            repo, run_id, call_id, resource, offset=offset, limit=limit
        )
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "ETag": f'"{metadata["reference"]}"',
                "X-Content-Bytes": str(metadata["total_bytes"]),
                "X-Content-Offset": str(metadata["offset"]),
                "X-Next-Offset": str(metadata["next_offset"])
                if metadata["next_offset"] is not None
                else "",
                "Content-Disposition": f'attachment; filename="{resource}.bin"',
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "no-store",
            },
        )

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
