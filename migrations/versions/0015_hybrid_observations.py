"""Hybrid 继承持久重执行约束，并独立保存模拟/录制/替换来源。"""

from alembic import context, op

from agentgit.storage.schema_s7_hybrid import build_metadata

revision = "0015_hybrid_observations"
down_revision = "0014_frozen_interventions"
branch_labels = None
depends_on = None


def trial_guard(modes: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION guard_experiment_trial() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM experiments e
                JOIN replay_requests p ON p.repository_id=e.repository_id
                    AND p.source_run_id=e.source_run_id AND p.from_commit_id=e.from_commit_id
                JOIN jobs j ON j.repository_id=p.repository_id AND j.id=p.job_id
                JOIN runs r ON r.repository_id=j.repository_id AND r.id=j.run_id
                WHERE e.repository_id=NEW.repository_id AND e.id=NEW.experiment_id
                    AND NEW.trial_index < e.repeats AND p.mode IN ({modes})
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
        END $$
    """


def upgrade() -> None:
    op.drop_constraint("ck_replay_requests_replay_mode", "replay_requests", type_="check")
    op.create_check_constraint(
        "ck_replay_requests_replay_mode", "replay_requests", "mode IN ('recorded','live','hybrid')"
    )
    build_metadata().tables["hybrid_observations"].create(op.get_bind(), checkfirst=False)
    op.execute(trial_guard("'live','hybrid'"))
    op.execute("""
        CREATE FUNCTION guard_hybrid_observation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM external_calls c
                JOIN runs r ON r.repository_id=c.repository_id AND r.id=c.run_id
                JOIN jobs j ON j.repository_id=r.repository_id AND j.run_id=r.id
                JOIN replay_requests p ON p.repository_id=j.repository_id AND p.job_id=j.id
                WHERE c.repository_id=NEW.repository_id AND c.id=NEW.call_id
                    AND c.run_id=NEW.run_id AND c.result_ref IS NOT NULL
                    AND c.origin_call_id IS NULL
                    AND ((c.effect='READ_ONLY'
                        AND c.provider IN ('tool:corpus_search/v1','tool:source_read/v1'))
                        OR (NEW.kind='miss' AND c.status='failed'))
                    AND r.mode='hybrid' AND p.mode='hybrid' AND j.id=NEW.job_id) THEN
                RAISE EXCEPTION 'hybrid observation must bind a complete readonly call of its job';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_hybrid_observation BEFORE INSERT ON hybrid_observations
            FOR EACH ROW EXECUTE FUNCTION guard_hybrid_observation();
        CREATE TRIGGER immutable_hybrid_observation BEFORE UPDATE OR DELETE ON hybrid_observations
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["hybrid_observations"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_hybrid_observation()")
    op.execute(trial_guard("'live'"))
    op.drop_constraint("ck_replay_requests_replay_mode", "replay_requests", type_="check")
    op.create_check_constraint(
        "ck_replay_requests_replay_mode", "replay_requests", "mode IN ('recorded','live')"
    )
