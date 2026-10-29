"""新 Job 类型约束只在测试 Schema 往返，已有 MCP 数据时拒绝降级。"""

import pytest
import sqlalchemy as sa
from alembic import command
from mcp_evidence import archive_mcp_run as archive_mcp_run
from test_mcp_adapter import server as server
from test_mcp_runs import case

pytestmark = pytest.mark.integration


def migration_config(connection):
    from test_kernel_migrations import config_for

    return config_for(connection)


def test_mcp_kind_migration_roundtrip_without_table_changes(history):
    with history.engine.begin() as connection:
        before = set(sa.inspect(connection).get_table_names())
        config = migration_config(connection)
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0027_mcp_runs"
        )
        command.downgrade(config, "0026_merge_runs")
        command.upgrade(config, "head")
        assert set(sa.inspect(connection).get_table_names()) == before


def test_mcp_jobs_cannot_be_discarded_by_downgrade(history, server, tmp_path):
    case(history, server, tmp_path)
    with history.engine.begin() as connection:
        config = migration_config(connection)
        with pytest.raises(RuntimeError, match="已有 MCP Job"):
            command.downgrade(config, "0026_merge_runs")
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0027_mcp_runs"
        )
