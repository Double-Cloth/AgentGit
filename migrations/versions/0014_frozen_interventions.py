"""不可变干预及其源事件、检查点、payload 与前置条件。"""

from alembic import context, op

from agentgit.storage.schema_s7_interventions import build_metadata

revision = "0014_frozen_interventions"
down_revision = "0013_branch_experiments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["interventions"].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_intervention_target() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM events e JOIN external_calls c
                ON c.repository_id=e.repository_id AND c.run_id=e.run_id
                    AND c.logical_step_id=e.logical_step_id
                WHERE e.id=NEW.target_event_id AND e.repository_id=NEW.repository_id
                    AND e.run_id=NEW.source_run_id AND e.seq=NEW.target_event_seq
                    AND e.type IN ('llm.requested','tool.started','call.reused')
                    AND c.id=NEW.source_call_id AND c.result_ref IS NOT NULL) THEN
                RAISE EXCEPTION 'intervention target must bind its source event and complete call';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_intervention_target BEFORE INSERT ON interventions
            FOR EACH ROW EXECUTE FUNCTION guard_intervention_target();
        CREATE TRIGGER immutable_intervention BEFORE UPDATE OR DELETE ON interventions
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
    """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    build_metadata().tables["interventions"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_intervention_target()")
