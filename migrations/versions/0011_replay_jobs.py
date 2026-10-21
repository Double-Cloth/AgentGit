"""耐久重放的来源与不可变检查点证明。"""

from alembic import context, op

from agentgit.storage.schema_s5_replays import build_metadata

revision = "0011_replay_jobs"
down_revision = "0010_branch_forks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = build_metadata().tables
    tables["replay_requests"].create(op.get_bind(), checkfirst=False)
    tables["replay_checkpoints"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_replay_request() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM jobs j JOIN runs r
                    ON r.id=j.run_id AND r.repository_id=j.repository_id
                JOIN branch_forks f
                    ON f.repository_id=r.repository_id AND f.branch_name=r.branch_name
                JOIN commits c ON c.repository_id=NEW.repository_id AND c.id=NEW.from_commit_id
                WHERE j.id=NEW.job_id AND j.repository_id=NEW.repository_id
                    AND j.origin_run_id IS NULL AND r.origin_run_id=NEW.source_run_id
                    AND r.base_commit_id=NEW.from_commit_id AND r.mode=NEW.mode
                    AND f.source_run_id=NEW.source_run_id AND f.from_commit_id=NEW.from_commit_id
                    AND c.environment_ref=NEW.environment_ref
            ) THEN
                RAISE EXCEPTION 'replay request does not match frozen job, source or fork';
            END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION guard_replay_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE request replay_requests%ROWTYPE;
                previous replay_checkpoints%ROWTYPE;
                parent_source text;
                parent_target text;
        BEGIN
            SELECT * INTO request FROM replay_requests
                WHERE job_id=NEW.job_id AND repository_id=NEW.repository_id;
            IF NOT FOUND OR request.mode <> 'recorded' THEN
                RAISE EXCEPTION 'checkpoint requires a recorded replay request';
            END IF;
            SELECT * INTO previous FROM replay_checkpoints WHERE job_id=NEW.job_id
                ORDER BY source_order DESC LIMIT 1;
            IF FOUND THEN
                IF NEW.source_order <> previous.source_order+1
                    OR previous.source_commit_id=request.until_commit_id THEN
                    RAISE EXCEPTION 'replay source order is not contiguous or exceeds until';
                END IF;
                parent_source := previous.source_commit_id;
                parent_target := previous.commit_id;
                IF NOT EXISTS (SELECT 1 FROM commit_parents
                    WHERE repository_id=NEW.repository_id AND commit_id=NEW.source_commit_id
                        AND parent_id=parent_source) THEN
                    RAISE EXCEPTION 'replay source path changed';
                END IF;
            ELSE
                IF NEW.source_order <> 0 OR NEW.source_commit_id <> request.from_commit_id THEN
                    RAISE EXCEPTION 'replay must start at its frozen from commit';
                END IF;
                parent_target := request.from_commit_id;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM commits source JOIN commits target
                    ON target.repository_id=source.repository_id
                        AND target.state_ref=source.state_ref
                JOIN run_commits rc
                    ON rc.repository_id=target.repository_id AND rc.commit_id=target.id
                JOIN runs r ON r.repository_id=rc.repository_id AND r.id=rc.run_id
                JOIN jobs j ON j.repository_id=r.repository_id AND j.run_id=r.id
                JOIN commit_parents p
                    ON p.repository_id=target.repository_id AND p.commit_id=target.id
                WHERE source.repository_id=NEW.repository_id AND source.id=NEW.source_commit_id
                    AND target.id=NEW.commit_id AND target.state_ref=NEW.state_ref
                    AND rc.run_id=NEW.run_id AND r.mode='recorded' AND j.id=NEW.job_id
                    AND p.parent_id=parent_target
                    AND (EXISTS (SELECT 1 FROM run_commits src
                        WHERE src.repository_id=NEW.repository_id
                            AND src.run_id=request.source_run_id
                            AND src.commit_id=NEW.source_commit_id)
                        OR EXISTS (SELECT 1 FROM runs src
                            WHERE src.repository_id=NEW.repository_id
                            AND src.id=request.source_run_id
                            AND src.base_commit_id=NEW.source_commit_id))
            ) THEN
                RAISE EXCEPTION 'replay checkpoint does not prove source state or target ownership';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_replay_request BEFORE INSERT ON replay_requests
            FOR EACH ROW EXECUTE FUNCTION guard_replay_request();
        CREATE TRIGGER immutable_replay_request BEFORE UPDATE OR DELETE ON replay_requests
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
        CREATE TRIGGER check_replay_checkpoint BEFORE INSERT ON replay_checkpoints
            FOR EACH ROW EXECUTE FUNCTION guard_replay_checkpoint();
        CREATE TRIGGER immutable_replay_checkpoint BEFORE UPDATE OR DELETE ON replay_checkpoints
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    tables = build_metadata().tables
    tables["replay_checkpoints"].drop(op.get_bind(), checkfirst=False)
    tables["replay_requests"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_replay_checkpoint(); DROP FUNCTION guard_replay_request()")
