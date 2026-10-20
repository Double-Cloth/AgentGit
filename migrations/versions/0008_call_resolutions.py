"""未知结果的可信核对与耐久证据。"""

from alembic import context, op

from agentgit.storage.schema_s4_resolutions import build_metadata

revision = "0008_call_resolutions"
down_revision = "0007_exact_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["call_resolutions"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE TRIGGER immutable_call_resolution BEFORE UPDATE OR DELETE ON call_resolutions
        FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["call_resolutions"].drop(op.get_bind(), checkfirst=False)
