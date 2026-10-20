"""精确审批及一次性调用消费。"""

from alembic import context, op

from agentgit.storage.schema_s4_approvals import build_metadata

revision = "0007_exact_approvals"
down_revision = "0006_job_budget_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["approvals"].create(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["approvals"].drop(op.get_bind(), checkfirst=False)
