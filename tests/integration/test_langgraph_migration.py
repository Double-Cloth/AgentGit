"""图 Job 类型扩展在隔离 Schema 往返；已有图任务时不能降级丢数据。"""

import pytest
import sqlalchemy as sa
from alembic import command
from test_kernel_migrations import config_for
from test_langgraph_runs import case
from test_mcp_adapter import server as server

pytestmark = pytest.mark.integration


def test_langgraph_kind_migration_roundtrip_changes_no_tables(history):
    with history.engine.begin() as connection:
        before = set(sa.inspect(connection).get_table_names())
        config = config_for(connection)
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0028_langgraph_runs"
        )
        command.downgrade(config, "0027_mcp_runs")
        command.upgrade(config, "head")
        assert set(sa.inspect(connection).get_table_names()) == before


def test_existing_langgraph_job_refuses_destructive_downgrade(history, server, tmp_path):
    case(history, server, tmp_path)
    with history.engine.begin() as connection:
        config = config_for(connection)
        with pytest.raises(RuntimeError, match="已有 LangGraph Job"):
            command.downgrade(config, "0027_mcp_runs")
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0028_langgraph_runs"
        )
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config_for(connection, testing=False), "0027_mcp_runs")
