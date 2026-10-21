"""为恢复链和复合 Recorded 的 Job 来源查询建立索引。"""

from alembic import context, op

revision = "0012_replay_source_index"
down_revision = "0011_replay_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_repository_origin", "jobs", ["repository_id", "origin_run_id"])


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.drop_index("ix_jobs_repository_origin", table_name="jobs")
