"""S2 进一步防止补写父关系与形成历史环。"""

from alembic import context, op

revision = "0003_parent_guards"
down_revision = "0002_version_kernel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION validate_parent_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.repository_id::text, 0));
            IF EXISTS (SELECT 1 FROM run_commits WHERE commit_id = NEW.commit_id) THEN
                RAISE EXCEPTION 'parent_history_already_published' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.parent_id
                    UNION
                    SELECT p.parent_id FROM commit_parents p JOIN ancestors a ON p.commit_id = a.id
                    WHERE p.repository_id = NEW.repository_id
                )
                SELECT 1 FROM ancestors WHERE id = NEW.commit_id
            ) THEN
                RAISE EXCEPTION 'parent_cycle' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER validate_parent_insert BEFORE INSERT ON commit_parents
        FOR EACH ROW EXECUTE FUNCTION validate_parent_insert()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.execute("DROP TRIGGER validate_parent_insert ON commit_parents")
    op.execute("DROP FUNCTION validate_parent_insert()")
