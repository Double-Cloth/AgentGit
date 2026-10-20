"""S2 的完整空库迁移和正式回退保护。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

pytestmark = pytest.mark.integration


def config_for(connection, *, testing=True):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes.update(testing=testing, connection=connection)
    return config


def test_full_kernel_migration_roundtrip_in_disposable_schema(history):
    with history.engine.begin() as connection:
        before = inspect(connection).get_table_names()
        config = config_for(connection)
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        assert inspect(connection).get_table_names() == before
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0003_parent_guards"
        )


def test_downgrade_requires_explicit_testing_mode(history):
    with history.engine.begin() as connection:
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config_for(connection, testing=False), "0002_version_kernel")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0003_parent_guards"
        )
