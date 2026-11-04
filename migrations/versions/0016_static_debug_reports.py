"""冻结静态报告与原始证据，不改写执行历史或触发执行。"""

from alembic import context, op

from agentgit.storage.schema_s8 import build_metadata

revision = "0016_static_debug_reports"
down_revision = "0015_hybrid_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["debug_reports"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_debug_report() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM object_refs r JOIN objects o ON o.id=r.object_id
                WHERE r.repository_id=NEW.repository_id AND r.owner_type='debug_report'
                    AND r.owner_id=NEW.id::text AND r.role='report'
                    AND r.object_id=NEW.report_ref AND o.object_type='blob') THEN
                RAISE EXCEPTION 'debug report must own its immutable payload';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_debug_report BEFORE INSERT ON debug_reports
            FOR EACH ROW EXECUTE FUNCTION guard_debug_report();
        CREATE TRIGGER immutable_debug_report BEFORE UPDATE OR DELETE ON debug_reports
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["debug_reports"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_debug_report()")
