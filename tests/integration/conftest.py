"""每个 S2 用例在真实测试库的独立 Schema 中运行迁移。"""

import re
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from agentgit.config import Settings, database_url
from agentgit.services.history import HistoryService
from agentgit.storage.database import make_engine
from agentgit.storage.objects import ObjectStore


@pytest.fixture
def history(tmp_path):
    settings = Settings.from_env()
    settings.assert_test_isolation()
    schema = "test_" + uuid4().hex
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    admin = make_engine(settings, testing=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["testing"] = True
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield HistoryService(engine, ObjectStore(tmp_path / "objects"))
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
