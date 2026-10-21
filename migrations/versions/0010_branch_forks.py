"""不可变 Fork 来源与操作者；不重写原分支或历史。"""

from alembic import context, op

from agentgit.storage.schema_s5_versions import build_metadata

revision = "0010_branch_forks"
down_revision = "0009_sandbox_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["branch_forks"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_branch_fork() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM branches b JOIN runs r ON r.repository_id=b.repository_id
                WHERE b.repository_id=NEW.repository_id AND b.name=NEW.branch_name
                    AND b.head_commit_id=NEW.from_commit_id AND b.version=0
                    AND b.active_writer_run_id IS NULL AND r.id=NEW.source_run_id
                    AND (r.base_commit_id=NEW.from_commit_id OR EXISTS (
                        SELECT 1 FROM run_commits rc WHERE rc.repository_id=NEW.repository_id
                            AND rc.run_id=NEW.source_run_id AND rc.commit_id=NEW.from_commit_id
                    ))
            ) THEN
                RAISE EXCEPTION 'fork provenance does not match the new branch or source path';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_branch_fork BEFORE INSERT ON branch_forks
        FOR EACH ROW EXECUTE FUNCTION guard_branch_fork();
        CREATE TRIGGER immutable_branch_fork BEFORE UPDATE OR DELETE ON branch_forks
        FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["branch_forks"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_branch_fork()")
