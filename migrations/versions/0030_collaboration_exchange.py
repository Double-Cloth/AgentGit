"""消息发送/接收和共享版本成为角色检查点的原子投影。"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import text

from agentgit.storage.schema_collaboration_exchange import TABLE_ORDER, build_metadata

revision = "0030_collaboration_exchange"
down_revision = "0029_collaboration_preparation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collaborations", sa.Column("failure_run_id", sa.Uuid))
    op.add_column("collaborations", sa.Column("failure_event_seq", sa.BigInteger))
    op.create_foreign_key(
        "fk_collaborations_failure_event",
        "collaborations",
        "events",
        ["repository_id", "failure_run_id", "failure_event_seq"],
        ["repository_id", "run_id", "seq"],
    )
    op.create_check_constraint(
        op.f("ck_collaborations_failure_cause"),
        "collaborations",
        "(failure_run_id IS NULL)=(failure_event_seq IS NULL)",
    )
    metadata = build_metadata()
    for name in TABLE_ORDER:
        metadata.tables[name].create(op.get_bind(), checkfirst=False)
        op.execute(
            f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )
    op.execute("""
        CREATE FUNCTION guard_collaboration_exchange() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME='collaboration_messages' THEN
                IF NOT EXISTS (SELECT 1 FROM collaboration_tasks s JOIN collaboration_tasks r
                    ON r.repository_id=s.repository_id AND r.collaboration_id=s.collaboration_id
                    WHERE s.repository_id=NEW.repository_id
                        AND s.collaboration_id=NEW.collaboration_id
                        AND s.run_id=NEW.sender_run_id AND r.run_id=NEW.receiver_run_id
                        AND r.id=NEW.receiver_task_id)
                    OR NOT EXISTS (SELECT 1 FROM snapshots s
                    WHERE s.repository_id=NEW.repository_id AND s.run_id=NEW.sender_run_id
                        AND s.commit_id=NEW.sent_commit_id AND s.event_seq>=NEW.sent_seq) THEN
                    RAISE EXCEPTION 'message requires exact role tasks and sender checkpoint';
                END IF;
            ELSIF TG_TABLE_NAME='collaboration_receipts' THEN
                IF NOT EXISTS (SELECT 1 FROM collaboration_messages m JOIN events e
                    ON e.repository_id=m.repository_id AND e.run_id=NEW.receiver_run_id
                        AND e.seq=NEW.received_seq
                    JOIN snapshots s ON s.repository_id=e.repository_id AND s.run_id=e.run_id
                        AND s.commit_id=NEW.received_commit_id AND s.event_seq>=e.seq
                    WHERE m.repository_id=NEW.repository_id AND m.id=NEW.message_id
                        AND m.receiver_run_id=NEW.receiver_run_id
                        AND e.caused_by_run_id=m.sender_run_id AND e.caused_by_seq=m.sent_seq) THEN
                    RAISE EXCEPTION 'receipt requires receiver checkpoint and exact send cause';
                END IF;
            ELSE
                IF NOT EXISTS (SELECT 1 FROM collaboration_tasks t JOIN snapshots s
                    ON s.repository_id=t.repository_id AND s.run_id=t.run_id
                    WHERE t.repository_id=NEW.repository_id
                        AND t.collaboration_id=NEW.collaboration_id
                        AND t.run_id=NEW.publisher_run_id AND s.commit_id=NEW.commit_id
                        AND s.event_seq>=NEW.event_seq) THEN
                    RAISE EXCEPTION 'publication requires own role checkpoint';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    for name in TABLE_ORDER:
        op.execute(
            f"CREATE TRIGGER owned_{name} BEFORE INSERT ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION guard_collaboration_exchange()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing"):
        raise RuntimeError("正式迁移禁止自动降级")
    if (
        op.get_bind()
        .execute(
            text("SELECT EXISTS(SELECT 1 FROM collaborations WHERE failure_run_id IS NOT NULL)")
        )
        .scalar()
    ):
        raise RuntimeError("已有协作停止因果，不能降级丢弃来源")
    for name in TABLE_ORDER:
        if op.get_bind().execute(text(f"SELECT EXISTS(SELECT 1 FROM {name})")).scalar():
            raise RuntimeError("已有协作消息或共享版本，不能降级丢弃来源")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_collaboration_exchange()")
    op.drop_constraint(op.f("ck_collaborations_failure_cause"), "collaborations", type_="check")
    op.drop_constraint("fk_collaborations_failure_event", "collaborations", type_="foreignkey")
    op.drop_column("collaborations", "failure_event_seq")
    op.drop_column("collaborations", "failure_run_id")
