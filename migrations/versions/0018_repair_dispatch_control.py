"""派发研究 Job 时再次串行核对搜索取消和控制租约。"""

from alembic import context, op

from agentgit.storage.schema_s9_dispatch import SEARCH_STATUSES, build_metadata

revision = "0018_repair_dispatch_control"
down_revision = "0017_repair_search_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["repair_trial_waits"].create(op.get_bind(), checkfirst=False)
    statuses = ",".join(f"'{value}'" for value in SEARCH_STATUSES)
    op.execute(f"""
        ALTER TABLE repair_jobs DROP CONSTRAINT ck_repair_jobs_repair_job_status;
        ALTER TABLE repair_jobs ADD CONSTRAINT ck_repair_jobs_repair_job_status
            CHECK (status IN ({statuses}));
        CREATE TRIGGER owned_repair_trial_wait BEFORE INSERT ON repair_trial_waits
            FOR EACH ROW EXECUTE FUNCTION
                guard_repair_owned_body('payload_ref','repair_trial','waiting');
        CREATE TRIGGER immutable_repair_trial_wait BEFORE UPDATE OR DELETE ON repair_trial_waits
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)
    op.execute("""
        CREATE FUNCTION guard_repair_dispatch_control() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE task repair_jobs%ROWTYPE;
        BEGIN
            SELECT j.* INTO task FROM repair_jobs j JOIN repair_trials t
                ON t.repository_id=j.repository_id AND t.search_id=j.search_id
                WHERE t.repository_id=NEW.repository_id AND t.id=NEW.id FOR UPDATE OF j;
            IF NOT FOUND OR task.status<>'running' OR task.cancel_requested
                OR task.lease_token IS NULL OR task.lease_until<=clock_timestamp() THEN
                RAISE EXCEPTION 'repair dispatch requires current uncancelled control ownership';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_repair_dispatch_control BEFORE INSERT ON repair_trial_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_repair_dispatch_control()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.execute("DROP TRIGGER check_repair_dispatch_control ON repair_trial_jobs")
    op.execute("DROP FUNCTION guard_repair_dispatch_control()")
    build_metadata().tables["repair_trial_waits"].drop(op.get_bind(), checkfirst=False)
    statuses = ",".join(
        f"'{value}'" for value in SEARCH_STATUSES if value != "waiting_reconciliation"
    )
    op.execute(f"""
        ALTER TABLE repair_jobs DROP CONSTRAINT ck_repair_jobs_repair_job_status;
        ALTER TABLE repair_jobs ADD CONSTRAINT ck_repair_jobs_repair_job_status
            CHECK (status IN ({statuses}))
    """)
