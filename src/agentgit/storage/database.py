"""短连接数据库探测，不在模块导入时连接数据库。"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from agentgit.config import Settings, database_url
from agentgit.core.errors import AgentGitError, ErrorCode


@contextmanager
def transaction(engine: Engine, connection: Connection | None = None) -> Iterator[Connection]:
    """已有用例事务由调用者提交，否则创建独立短事务。"""
    if connection is not None:
        yield connection
    else:
        with engine.begin() as opened:
            yield opened


def make_engine(settings: Settings, *, testing: bool = False) -> Engine:
    if testing:
        settings.assert_test_isolation()
    url = database_url(settings.test_database_url if testing else settings.database_url)
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.connect_timeout_seconds},
        hide_parameters=True,
    )


def probe_database(settings: Settings, *, testing: bool = False) -> str:
    engine = make_engine(settings, testing=testing)
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("SHOW server_version")).scalar_one())
    except SQLAlchemyError:
        raise AgentGitError(
            ErrorCode.DATABASE_UNAVAILABLE, "PostgreSQL 不可用，请检查本地服务"
        ) from None
    finally:
        engine.dispose()
