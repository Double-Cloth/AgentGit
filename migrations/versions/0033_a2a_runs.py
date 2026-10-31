"""A2A 程序复用现有队列，保留已有协议类型和数据。"""

from alembic import context, op
from sqlalchemy import text

revision = "0033_a2a_runs"
down_revision = "0032_collab_recorded_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind"),
        "jobs",
        "kind IN ('research_run','mcp_run','langgraph_run','collaboration_role','a2a_run')",
    )


def downgrade() -> None:
    if not context.config.attributes.get("testing"):
        raise RuntimeError("正式迁移禁止自动降级")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='a2a_run')"))
        .scalar()
    ):
        raise RuntimeError("已有 A2A Job，不能丢弃类型约束或数据")
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind"),
        "jobs",
        "kind IN ('research_run','mcp_run','langgraph_run','collaboration_role')",
    )
