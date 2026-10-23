"""冻结实验 manifest 和每次独立 Job/Run 的归属。"""

from alembic import context, op

from agentgit.storage.schema_s7_experiments import build_metadata

revision = "0013_branch_experiments"
down_revision = "0012_replay_source_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = build_metadata().tables
    tables["experiments"].create(op.get_bind(), checkfirst=False)
    tables["experiment_trials"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_experiment_trial() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM experiments e
                JOIN replay_requests p ON p.repository_id=e.repository_id
                    AND p.source_run_id=e.source_run_id AND p.from_commit_id=e.from_commit_id
                JOIN jobs j ON j.repository_id=p.repository_id AND j.id=p.job_id
                JOIN runs r ON r.repository_id=j.repository_id AND r.id=j.run_id
                WHERE e.repository_id=NEW.repository_id AND e.id=NEW.experiment_id
                    AND NEW.trial_index < e.repeats AND p.mode='live'
                    AND j.id=NEW.job_id AND j.run_id=NEW.initial_run_id
                    AND j.origin_run_id IS NULL AND j.status='queued'
                    AND r.origin_run_id=e.source_run_id AND r.base_commit_id=e.from_commit_id
                    AND r.branch_name=NEW.branch_name
                    AND NEW.branch_name='exp-' || replace(e.id::text, '-', '')
                        || '-' || NEW.group_key || '-' || NEW.trial_index::text
            ) THEN
                RAISE EXCEPTION 'trial must own a new queued job at the frozen experiment base';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_experiment_trial BEFORE INSERT ON experiment_trials
            FOR EACH ROW EXECUTE FUNCTION guard_experiment_trial();
        CREATE TRIGGER immutable_experiment BEFORE UPDATE OR DELETE ON experiments
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
        CREATE TRIGGER immutable_experiment_trial BEFORE UPDATE OR DELETE ON experiment_trials
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    tables = build_metadata().tables
    tables["experiment_trials"].drop(op.get_bind(), checkfirst=False)
    tables["experiments"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_experiment_trial()")
