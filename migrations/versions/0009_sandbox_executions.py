"""调用意图原子绑定固定容器，保留清理与资源操作证据。"""

from alembic import context, op

from agentgit.storage.schema_s4_executions import build_metadata

revision = "0009_sandbox_executions"
down_revision = "0008_call_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["sandbox_executions"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_sandbox_execution() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'execution records cannot be deleted';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status = 'complete' OR ROW(
                    NEW.call_id, NEW.repository_id, NEW.run_id, NEW.job_id,
                    NEW.checkpoint_commit_id, NEW.request_digest, NEW.policy_ref,
                    NEW.input_manifest_ref, NEW.image_id, NEW.container_name, NEW.attempt,
                    NEW.fencing_token, NEW.effective_timeout_ms, NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.call_id, OLD.repository_id, OLD.run_id, OLD.job_id,
                    OLD.checkpoint_commit_id, OLD.request_digest, OLD.policy_ref,
                    OLD.input_manifest_ref, OLD.image_id, OLD.container_name, OLD.attempt,
                    OLD.fencing_token, OLD.effective_timeout_ms, OLD.created_at
                ) THEN
                    RAISE EXCEPTION 'execution identity or completed result is immutable';
                END IF;
            ELSE
                IF NOT EXISTS (
                    SELECT 1 FROM external_calls c JOIN jobs j ON j.id = NEW.job_id
                    WHERE c.id = NEW.call_id AND c.repository_id = NEW.repository_id
                        AND c.run_id = NEW.run_id AND c.request_digest = NEW.request_digest
                        AND c.attempt = NEW.attempt AND c.effect = 'NON_IDEMPOTENT_WRITE'
                        AND c.provider IN ('tool:terminal_exec/v1','tool:code_exec/v1')
                        AND c.status = 'started' AND j.repository_id = NEW.repository_id
                        AND j.run_id = NEW.run_id AND j.fencing_token = NEW.fencing_token
                        AND j.status = 'running' AND j.lease_until > clock_timestamp()
                ) THEN
                    RAISE EXCEPTION 'execution does not match an admitted leased call';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER protect_sandbox_execution BEFORE INSERT OR UPDATE OR DELETE
        ON sandbox_executions FOR EACH ROW EXECUTE FUNCTION guard_sandbox_execution();
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["sandbox_executions"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_sandbox_execution()")
