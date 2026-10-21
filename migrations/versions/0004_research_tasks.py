"""S3 不可变配置与版本化任务计划。"""

import sqlalchemy as sa
from alembic import context, op

from agentgit.storage.schema_s3 import build_metadata

revision = "0004_research_tasks"
down_revision = "0003_parent_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("task_id", sa.Uuid))
    op.add_column("runs", sa.Column("agent_config_id", sa.Uuid))
    op.create_unique_constraint("uq_runs_task", "runs", ["repository_id", "task_id", "id"])
    metadata = build_metadata()
    metadata.create_all(
        op.get_bind(),
        tables=[metadata.tables[name] for name in ("agent_configs", "tasks", "task_nodes")],
        checkfirst=False,
    )
    op.create_foreign_key(
        "fk_runs_task", "runs", "tasks", ["repository_id", "task_id"], ["repository_id", "id"]
    )
    op.create_foreign_key(
        "fk_runs_agent_config",
        "runs",
        "agent_configs",
        ["repository_id", "agent_config_id"],
        ["repository_id", "id"],
    )
    op.create_index("ix_runs_task", "runs", ["repository_id", "task_id", "created_at"])
    op.create_index("ix_runs_agent_config", "runs", ["repository_id", "agent_config_id"])
    op.execute(
        "CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON agent_configs "
        "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
    )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.drop_index("ix_runs_agent_config", "runs")
    op.drop_index("ix_runs_task", "runs")
    op.drop_constraint("fk_runs_agent_config", "runs", type_="foreignkey")
    op.drop_constraint("fk_runs_task", "runs", type_="foreignkey")
    metadata = build_metadata()
    metadata.drop_all(
        op.get_bind(),
        tables=[metadata.tables[name] for name in ("task_nodes", "tasks", "agent_configs")],
        checkfirst=False,
    )
    op.drop_constraint("uq_runs_task", "runs", type_="unique")
    op.drop_column("runs", "agent_config_id")
    op.drop_column("runs", "task_id")
