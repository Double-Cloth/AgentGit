"""Recorded 四角色来源关联与独立准备；历史迁移保持原样。"""

from alembic import context, op
from sqlalchemy import text

from agentgit.storage.schema_collaboration_recordings import TABLE_ORDER, build_metadata

revision = "0031_collaboration_recordings"
down_revision = "0030_collaboration_exchange"
branch_labels = None
depends_on = None


def _task_guard(recorded: bool) -> None:
    mode = "r.mode='live' AND r.origin_run_id IS NULL"
    if recorded:
        mode = """((r.mode='live' AND r.origin_run_id IS NULL AND j.origin_run_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM collaboration_recordings x
                WHERE x.repository_id=NEW.repository_id
                    AND x.collaboration_id=NEW.collaboration_id))
            OR (r.mode='recorded' AND r.origin_run_id IS NOT NULL
            AND j.origin_run_id=r.origin_run_id
            AND EXISTS (SELECT 1 FROM collaboration_recordings x
                JOIN collaboration_tasks t ON t.repository_id=x.repository_id
                    AND t.collaboration_id=x.source_collaboration_id
                JOIN runs s ON s.repository_id=t.repository_id AND s.id=t.run_id
                WHERE x.repository_id=NEW.repository_id
                    AND x.collaboration_id=NEW.collaboration_id
                    AND t.role=NEW.role AND t.run_id=r.origin_run_id
                    AND s.mode='live' AND s.origin_run_id IS NULL
                    AND s.base_commit_id IS NULL
                    AND s.status='completed' AND t.policy_ref=NEW.policy_ref
                    AND t.budget_ref=NEW.budget_ref
                    AND t.private_input_ref=NEW.private_input_ref)))"""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION guard_collaboration_task() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM jobs j JOIN runs r
                ON r.repository_id=j.repository_id AND r.id=j.run_id
                WHERE j.repository_id=NEW.repository_id AND j.id=NEW.job_id
                    AND j.run_id=NEW.run_id AND j.kind='collaboration_role'
                    AND j.status='paused' AND r.status='paused'
                    AND r.task_id IS NULL AND r.agent_config_id IS NULL
                    AND ({mode}) AND r.base_commit_id IS NULL
                    AND r.checkpoint_commit_id IS NULL) THEN
                RAISE EXCEPTION 'collaboration requires independent prepared role job/run';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM object_refs x
                WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration_task'
                    AND x.owner_id=NEW.id::text AND x.role='private_input'
                    AND x.object_id=NEW.private_input_ref)
                OR NOT EXISTS (SELECT 1 FROM object_refs x
                WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration_task'
                    AND x.owner_id=NEW.id::text AND x.role='policy' AND x.object_id=NEW.policy_ref)
                OR NOT EXISTS (SELECT 1 FROM object_refs x
                WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration_task'
                    AND x.owner_id=NEW.id::text AND x.role='budget'
                    AND x.object_id=NEW.budget_ref) THEN
                RAISE EXCEPTION 'collaboration task requires exact owned inputs';
            END IF;
            RETURN NEW;
        END $$;
    """)


def upgrade() -> None:
    metadata = build_metadata()
    for name in TABLE_ORDER:
        metadata.tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_collaboration_recording() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM collaborations n JOIN collaborations s
                ON s.repository_id=n.repository_id AND s.id=NEW.source_collaboration_id
                WHERE n.repository_id=NEW.repository_id AND n.id=NEW.collaboration_id
                    AND n.id<>s.id AND n.status='prepared' AND s.status='completed'
                    AND s.failure_run_id IS NULL AND s.failure_event_seq IS NULL
                    AND n.actor=s.actor AND n.program_ref=s.program_ref)
                OR NOT EXISTS (SELECT 1 FROM object_refs x
                    WHERE x.repository_id=NEW.repository_id
                        AND x.owner_type='collaboration_recording'
                        AND x.owner_id=NEW.collaboration_id::text AND x.role='source_index'
                        AND x.object_id=NEW.source_index_ref)
                OR NOT EXISTS (SELECT 1 FROM object_refs x
                    WHERE x.repository_id=NEW.repository_id
                        AND x.owner_type='collaboration_recording'
                        AND x.owner_id=NEW.collaboration_id::text AND x.role='identities'
                        AND x.object_id=NEW.identity_map_ref) THEN
                RAISE EXCEPTION 'recorded collaboration requires actual successful owned source';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER owned_collaboration_recording BEFORE INSERT ON collaboration_recordings
            FOR EACH ROW EXECUTE FUNCTION guard_collaboration_recording();
        CREATE TRIGGER immutable_collaboration_recording
            BEFORE UPDATE OR DELETE ON collaboration_recordings
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
        CREATE FUNCTION check_recorded_role_set() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (SELECT count(*) FROM collaboration_tasks t JOIN runs r
                ON r.repository_id=t.repository_id AND r.id=t.run_id
                JOIN collaboration_tasks s ON s.repository_id=t.repository_id
                    AND s.collaboration_id=NEW.source_collaboration_id AND s.role=t.role
                WHERE t.repository_id=NEW.repository_id AND t.collaboration_id=NEW.collaboration_id
                    AND r.mode='recorded' AND r.origin_run_id=s.run_id) <> 4 THEN
                RAISE EXCEPTION 'recorded collaboration must atomically contain four source roles';
            END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER complete_recorded_roles AFTER INSERT ON collaboration_recordings
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_recorded_role_set();
    """)
    _task_guard(True)


def downgrade() -> None:
    if not context.config.attributes.get("testing"):
        raise RuntimeError("正式迁移禁止自动降级")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS(SELECT 1 FROM collaboration_recordings)"))
        .scalar()
    ):
        raise RuntimeError("已有协作 Recorded 来源，不能降级丢弃关联")
    _task_guard(False)
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_collaboration_recording()")
    op.execute("DROP FUNCTION check_recorded_role_set()")
