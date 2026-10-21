"""S1 迁移框架基线，业务表从 S2 引入。"""

from alembic import context

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("只允许在显式选择的可丢弃测试库回退")
