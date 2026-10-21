"""S4 持久化 Job、控制请求与分支活动写者。"""

import sqlalchemy as sa
from alembic import context, op

from agentgit.storage.schema_s4_jobs import build_metadata

revision = "0005_durable_jobs"
down_revision = "0004_research_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("state_version", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("control_request", sa.String(20)),
        sa.Column("control_status", sa.String(20)),
        sa.Column("failure_code", sa.String(100)),
    ):
        op.add_column("runs", column)
    op.add_column("branches", sa.Column("active_writer_run_id", sa.Uuid))
    op.create_foreign_key(
        "fk_branch_writer",
        "branches",
        "runs",
        ["repository_id", "active_writer_run_id"],
        ["repository_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    metadata = build_metadata()
    metadata.create_all(
        op.get_bind(),
        tables=[metadata.tables[name] for name in ("jobs", "idempotency_keys")],
        checkfirst=False,
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM runs
                       WHERE status NOT IN ('completed','failed','cancelled','budget_exhausted')
                       GROUP BY repository_id, branch_name HAVING count(*) > 1) THEN
                RAISE EXCEPTION 'multiple_active_writers_require_reconciliation';
            END IF;
        END $$
    """)
    op.execute("""
        UPDATE branches b SET active_writer_run_id = r.id FROM runs r
        WHERE r.repository_id = b.repository_id AND r.branch_name = b.name
          AND r.status NOT IN ('completed','failed','cancelled','budget_exhausted')
    """)
    op.execute("""
        CREATE FUNCTION claim_branch_writer() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status NOT IN ('completed','failed','cancelled','budget_exhausted') THEN
                UPDATE branches SET active_writer_run_id = NEW.id
                WHERE repository_id = NEW.repository_id AND name = NEW.branch_name
                  AND (active_writer_run_id IS NULL OR active_writer_run_id = NEW.id);
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'branch_writer_conflict' USING ERRCODE = '23514';
                END IF;
            ELSE
                UPDATE branches SET active_writer_run_id = NULL
                WHERE repository_id = NEW.repository_id AND name = NEW.branch_name
                  AND active_writer_run_id = NEW.id;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER branch_writer BEFORE INSERT OR UPDATE OF status ON runs "
        "FOR EACH ROW EXECUTE FUNCTION claim_branch_writer()"
    )
    op.execute("""
        CREATE FUNCTION bump_run_state_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.state_version = OLD.state_version + 1; RETURN NEW; END $$
    """)
    op.execute(
        "CREATE TRIGGER run_state_version BEFORE UPDATE OF status, checkpoint_commit_id, "
        "control_request ON runs FOR EACH ROW EXECUTE FUNCTION bump_run_state_version()"
    )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.execute("DROP TRIGGER run_state_version ON runs")
    op.execute("DROP FUNCTION bump_run_state_version()")
    op.execute("DROP TRIGGER branch_writer ON runs")
    op.execute("DROP FUNCTION claim_branch_writer()")
    metadata = build_metadata()
    metadata.drop_all(
        op.get_bind(),
        tables=[metadata.tables[name] for name in ("idempotency_keys", "jobs")],
        checkfirst=False,
    )
    op.drop_constraint("fk_branch_writer", "branches", type_="foreignkey")
    op.drop_column("branches", "active_writer_run_id")
    for name in ("failure_code", "control_status", "control_request", "state_version"):
        op.drop_column("runs", name)
