"""只在测试 Schema 往返新迁移，旧表、约束和触发器保持兼容。"""

import pytest
from alembic import command
from sqlalchemy import inspect, text
from test_kernel_migrations import config_for, schema_signature

from agentgit.storage.schema_s11_proposals import TABLE_ORDER

pytestmark = pytest.mark.integration


def test_merge_schema_round_trip_and_production_downgrade_guard(history):
    with history.engine.begin() as connection:
        config = config_for(connection)
        before = schema_signature(connection)
        assert set(TABLE_ORDER) <= set(inspect(connection).get_table_names())
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0024_merge_proposals"
        )
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config_for(connection, testing=False), "0023_repair_proposal_reviews")
        command.downgrade(config, "0023_repair_proposal_reviews")
        assert not set(TABLE_ORDER) & set(inspect(connection).get_table_names())
        command.upgrade(config, "head")
        assert schema_signature(connection) == before
