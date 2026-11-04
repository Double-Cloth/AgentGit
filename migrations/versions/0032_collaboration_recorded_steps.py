"""Recorded 新历史与来源检查点同事务落账。"""

from alembic import context, op
from sqlalchemy import text

from agentgit.storage.schema_collaboration_recorded_steps import TABLE_ORDER, build_metadata

revision = "0032_collab_recorded_steps"
down_revision = "0031_collaboration_recordings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = build_metadata()
    for name in TABLE_ORDER:
        metadata.tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_collaboration_recorded_step() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM collaboration_tasks t JOIN runs r
                ON r.repository_id=t.repository_id AND r.id=t.run_id
                JOIN collaboration_recordings x ON x.repository_id=t.repository_id
                    AND x.collaboration_id=t.collaboration_id
                JOIN collaboration_tasks s ON s.repository_id=x.repository_id
                    AND s.collaboration_id=x.source_collaboration_id AND s.role=t.role
                JOIN snapshots a ON a.repository_id=t.repository_id AND a.run_id=t.run_id
                    AND a.commit_id=NEW.commit_id
                JOIN snapshots b ON b.repository_id=s.repository_id AND b.run_id=s.run_id
                    AND b.commit_id=NEW.source_commit_id
                WHERE t.repository_id=NEW.repository_id AND t.run_id=NEW.run_id
                    AND r.mode='recorded' AND r.origin_run_id=NEW.source_run_id
                    AND s.run_id=NEW.source_run_id AND x.source_index_ref=NEW.source_index_ref) THEN
                RAISE EXCEPTION 'recorded step requires actual own source and target snapshots';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER owned_collaboration_recorded_step
            BEFORE INSERT ON collaboration_recorded_steps
            FOR EACH ROW EXECUTE FUNCTION guard_collaboration_recorded_step();
        CREATE TRIGGER immutable_collaboration_recorded_step
            BEFORE UPDATE OR DELETE ON collaboration_recorded_steps
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing"):
        raise RuntimeError("正式迁移禁止自动降级")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS(SELECT 1 FROM collaboration_recordings)"))
        .scalar()
    ):
        raise RuntimeError("已有协作 Recorded 来源或进度，不能降级")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_collaboration_recorded_step()")
