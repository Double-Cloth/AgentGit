"""固定 LangGraph 复用已有耐久队列，不新增业务表。"""

from alembic import context, op
from sqlalchemy import text

revision = "0028_langgraph_runs"
down_revision = "0027_mcp_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind"), "jobs", "kind IN ('research_run','mcp_run','langgraph_run')"
    )


def downgrade() -> None:
    if not context.config.attributes.get("testing"):
        raise RuntimeError("正式迁移禁止自动降级")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='langgraph_run')"))
        .scalar()
    ):
        raise RuntimeError("已有 LangGraph Job，不能删除其类型约束或丢弃数据")
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind"), "jobs", "kind IN ('research_run','mcp_run')")
