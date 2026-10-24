"""小连接池与同键并发的真实 PostgreSQL 边界。"""

import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_debug_reports import failed_case as failed_case

from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService

pytestmark = pytest.mark.integration


@contextmanager
def small_pool(history, tmp_path, size=1):
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    engine = sa.create_engine(
        history.engine.url,
        pool_size=size,
        max_overflow=0,
        pool_timeout=1,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        bounded = HistoryService(engine, history.objects)
        yield ManagementService(ResearchService(bounded, tmp_path / "small-pool"))
    finally:
        engine.dispose()


def test_report_create_works_with_one_connection_without_overflow(
    history, failed_case, tmp_path, monkeypatch
):
    _, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    with small_pool(history, tmp_path) as manager:
        original = manager.debug_reports.compile

        def compile_without_publishing_connection(*args, **kwargs):
            assert manager.history.engine.pool.checkedout() == 0
            return original(*args, **kwargs)

        monkeypatch.setattr(manager.debug_reports, "compile", compile_without_publishing_connection)
        result = manager.create_debug_report(repo, request, "single-pool")
        assert (
            manager.debug_report(repo, UUID(result["debug_report_id"]))["report"]["diagnosis"][
                "mode"
            ]
            == "static_only"
        )


def test_same_key_parallel_creation_publishes_one_report_with_small_pool(
    history, failed_case, tmp_path
):
    _, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    with small_pool(history, tmp_path, size=2) as manager:
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(
                workers.map(
                    lambda _: manager.create_debug_report(repo, request, "parallel-report"),
                    range(6),
                )
            )
        assert all(item == results[0] for item in results)
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["debug_reports"])
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar_one()
            == 0
        )


def test_concurrent_same_key_different_request_conflicts_after_readonly_preparation(
    history, failed_case, tmp_path, monkeypatch
):
    _, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    alternate = NewDebugReport(
        failed_run_id=proof["failed_run_id"], reference_run_id=proof["reference_run_id"]
    )
    barrier = Barrier(2)
    with small_pool(history, tmp_path, size=2) as manager:
        original = manager.debug_reports.compile

        def synchronized(*args, **kwargs):
            barrier.wait(timeout=15)
            return original(*args, **kwargs)

        monkeypatch.setattr(manager.debug_reports, "compile", synchronized)

        def create(payload):
            try:
                return manager.create_debug_report(repo, payload, "competing-report")
            except AgentGitError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(create, (request, alternate)))
        assert sum(isinstance(item, dict) for item in results) == 1
        assert results.count(ErrorCode.VERSION_CONFLICT) == 1
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["debug_reports"])
            ).scalar_one()
            == 1
        )


def test_publication_failure_rolls_back_report_owner_and_idempotency_then_same_key_retries(
    history, failed_case, tmp_path, monkeypatch
):
    _, proof, request = failed_case
    repo = UUID(proof["repository_id"])
    with small_pool(history, tmp_path) as manager:
        original = manager.debug_reports.store

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise AgentGitError(ErrorCode.INTERNAL_ERROR, "发布事务故障注入")

        with monkeypatch.context() as failure:
            failure.setattr(manager.debug_reports, "store", interrupted)
            with pytest.raises(AgentGitError):
                manager.create_debug_report(repo, request, "retry-publication")
        with manager.history.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["debug_reports"])
                ).scalar_one()
                == 0
            )
            refs = TABLES["object_refs"]
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(refs)
                    .where(refs.c.owner_type == "debug_report")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(TABLES["idempotency_keys"])
                ).scalar_one()
                == 0
            )
        created = manager.create_debug_report(repo, request, "retry-publication")
        assert manager.create_debug_report(repo, request, "retry-publication") == created
