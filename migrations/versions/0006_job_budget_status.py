"""允许 Job 如实记录预算耗尽；不重写已应用的调度基线。"""

from alembic import context, op

revision = "0006_job_budget_status"
down_revision = "0005_durable_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status IN ('queued','running','paused','waiting_approval','completed',"
        "'failed','cancelled','outcome_unknown','budget_exhausted')",
    )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status IN ('queued','running','paused','waiting_approval','completed',"
        "'failed','cancelled','outcome_unknown')",
    )
