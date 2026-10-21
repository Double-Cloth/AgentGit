"""S2 版本内核与同项目复合外键。"""

from alembic import context, op

from agentgit.storage.schema_s2 import build_metadata

revision = "0002_version_kernel"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().create_all(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable_history';
        END;
        $$
    """)
    for table in (
        "objects",
        "object_refs",
        "commits",
        "commit_parents",
        "events",
        "run_commits",
        "snapshots",
        "artifacts",
    ):
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().drop_all(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION reject_history_mutation()")
