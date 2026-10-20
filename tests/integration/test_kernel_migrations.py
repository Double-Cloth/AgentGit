"""S2 的完整空库迁移和正式回退保护。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

pytestmark = pytest.mark.integration


def config_for(connection, *, testing=True):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes.update(testing=testing, connection=connection)
    return config


def schema_signature(connection):
    inspector = inspect(connection)
    result = {}
    for name in sorted(inspector.get_table_names()):
        result[name] = {
            "columns": [
                (
                    column["name"],
                    str(column["type"]),
                    column["nullable"],
                    column["default"],
                    column.get("identity"),
                )
                for column in inspector.get_columns(name)
            ],
            "primary_key": inspector.get_pk_constraint(name),
            "foreign_keys": sorted(
                inspector.get_foreign_keys(name), key=lambda value: value["name"]
            ),
            "unique": sorted(
                inspector.get_unique_constraints(name), key=lambda value: value["name"]
            ),
            "checks": sorted(
                inspector.get_check_constraints(name), key=lambda value: value["name"]
            ),
            "indexes": sorted(inspector.get_indexes(name), key=lambda value: value["name"]),
        }
    return result


def test_full_kernel_migration_roundtrip_in_disposable_schema(history):
    with history.engine.begin() as connection:
        before = schema_signature(connection)
        config = config_for(connection)
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        assert schema_signature(connection) == before
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == ScriptDirectory.from_config(config).get_current_head()
        )


def test_downgrade_requires_explicit_testing_mode(history):
    with history.engine.begin() as connection:
        config = config_for(connection, testing=False)
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config, "0002_version_kernel")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == ScriptDirectory.from_config(config).get_current_head()
        )
