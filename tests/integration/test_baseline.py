"""S1-T01/T02：只在可丢弃的真实测试库做迁移往返。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.storage.database import make_engine, probe_database

pytestmark = pytest.mark.integration


def test_postgres_migration_roundtrip():
    settings = Settings.from_env()
    settings.assert_test_isolation()
    assert probe_database(settings, testing=True)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["testing"] = True
    engine = make_engine(settings, testing=True)
    try:
        command.upgrade(config, "0001_baseline")
        with engine.connect() as connection:
            before = inspect(connection).get_table_names()
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 0
            )
        command.upgrade(config, "0001_baseline")
        with engine.connect() as connection:
            assert inspect(connection).get_table_names() == before
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def test_postgres_readiness():
    settings = Settings.from_env()
    settings.assert_test_isolation()
    config = settings.model_copy(update={"database_url": settings.test_database_url})
    assert TestClient(create_app(config)).get("/ready").status_code == 200
