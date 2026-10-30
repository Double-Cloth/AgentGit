"""协作组和四个独立角色任务的耐久准备；不自动开始执行。"""

from alembic import context, op
from sqlalchemy import text

from agentgit.storage.schema_collaboration import TABLE_ORDER, build_metadata

revision = "0029_collaboration_preparation"
down_revision = "0028_langgraph_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind"),
        "jobs",
        "kind IN ('research_run','mcp_run','langgraph_run','collaboration_role')",
    )
    metadata = build_metadata()
    for name in TABLE_ORDER:
        metadata.tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_collaboration_task() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM jobs j JOIN runs r
                ON r.repository_id=j.repository_id AND r.id=j.run_id
                WHERE j.repository_id=NEW.repository_id AND j.id=NEW.job_id
                    AND j.run_id=NEW.run_id AND j.kind='collaboration_role'
                    AND j.status='paused' AND r.status='paused'
                    AND r.task_id IS NULL AND r.agent_config_id IS NULL
                    AND r.mode='live' AND r.origin_run_id IS NULL
                    AND r.base_commit_id IS NULL AND r.checkpoint_commit_id IS NULL) THEN
                RAISE EXCEPTION 'collaboration requires independent prepared role job/run';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM object_refs x
                WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration_task'
                    AND x.owner_id=NEW.id::text AND x.role='private_input'
                    AND x.object_id=NEW.private_input_ref)
                OR NOT EXISTS (SELECT 1 FROM object_refs x
                WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration_task'
                    AND x.owner_id=NEW.id::text AND x.role='policy'
                    AND x.object_id=NEW.policy_ref)
                OR NOT EXISTS (SELECT 1 FROM object_refs x
                WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration_task'
                    AND x.owner_id=NEW.id::text AND x.role='budget'
                    AND x.object_id=NEW.budget_ref) THEN
                RAISE EXCEPTION 'collaboration task requires exact owned inputs';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER owned_collaboration_task BEFORE INSERT ON collaboration_tasks
            FOR EACH ROW EXECUTE FUNCTION guard_collaboration_task();
        CREATE TRIGGER immutable_collaboration_task BEFORE UPDATE OR DELETE ON collaboration_tasks
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
        CREATE FUNCTION guard_collaboration_group() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NOT EXISTS (SELECT 1 FROM object_refs x
                    WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration'
                        AND x.owner_id=NEW.id::text AND x.role='program'
                        AND x.object_id=NEW.program_ref)
                    OR NOT EXISTS (SELECT 1 FROM object_refs x
                    WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration'
                        AND x.owner_id=NEW.id::text AND x.role='bindings'
                        AND x.object_id=NEW.bindings_ref)
                    OR NOT EXISTS (SELECT 1 FROM object_refs x
                    WHERE x.repository_id=NEW.repository_id AND x.owner_type='collaboration'
                        AND x.owner_id=NEW.id::text AND x.role='environment'
                        AND x.object_id=NEW.environment_ref) THEN
                    RAISE EXCEPTION 'collaboration group requires exact owned inputs';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP='DELETE' OR (OLD.id,OLD.repository_id,OLD.program_ref,OLD.bindings_ref,
                OLD.environment_ref,OLD.actor,OLD.created_at) IS DISTINCT FROM
                (NEW.id,NEW.repository_id,NEW.program_ref,NEW.bindings_ref,
                NEW.environment_ref,NEW.actor,NEW.created_at) THEN
                RAISE EXCEPTION 'collaboration frozen identity cannot change';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER immutable_collaboration_group
            BEFORE INSERT OR UPDATE OR DELETE ON collaborations
            FOR EACH ROW EXECUTE FUNCTION guard_collaboration_group();
        CREATE FUNCTION check_collaboration_complete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (SELECT count(*) FROM collaboration_tasks t
                WHERE t.repository_id=NEW.repository_id AND t.collaboration_id=NEW.id) <> 4 THEN
                RAISE EXCEPTION 'collaboration must atomically contain four role tasks';
            END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER complete_collaboration AFTER INSERT ON collaborations
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION check_collaboration_complete();
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing"):
        raise RuntimeError("正式迁移禁止自动降级")
    if (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM collaborations) OR "
                "EXISTS(SELECT 1 FROM collaboration_tasks) OR "
                "EXISTS(SELECT 1 FROM jobs WHERE kind='collaboration_role')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("已有协作数据，不能降级丢弃四角色来源")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    for name in (
        "guard_collaboration_task",
        "guard_collaboration_group",
        "check_collaboration_complete",
    ):
        op.execute(f"DROP FUNCTION {name}()")
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind"), "jobs", "kind IN ('research_run','mcp_run','langgraph_run')"
    )
