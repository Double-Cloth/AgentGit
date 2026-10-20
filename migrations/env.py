"""默认只前进；测试库回退必须显式选择 testing。"""

from alembic import context

from agentgit.config import Settings, database_url
from agentgit.storage.database import make_engine

config = context.config
settings = Settings.from_env()
testing = config.attributes.get("testing", False)
if testing:
    settings.assert_test_isolation()


def run_migrations() -> None:
    if context.is_offline_mode():
        url = database_url(settings.test_database_url if testing else settings.database_url)
        context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
        with context.begin_transaction():
            context.run_migrations()
        return
    provided = config.attributes.get("connection")
    if provided is not None:
        context.configure(connection=provided, target_metadata=None, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(settings, testing=testing)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
