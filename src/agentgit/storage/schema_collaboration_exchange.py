"""不可变消息、接收收据和共享版本；引用实际角色 Run 与耐久事件。"""

import sqlalchemy as sa

from agentgit.storage.schema_collaboration import build_metadata as build_previous

TABLE_ORDER = ("collaboration_messages", "collaboration_receipts", "collaboration_publications")


def build_metadata() -> sa.MetaData:
    metadata = build_previous()
    groups = metadata.tables["collaborations"]
    groups.append_column(sa.Column("failure_run_id", sa.Uuid))
    groups.append_column(sa.Column("failure_event_seq", sa.BigInteger))
    groups.append_constraint(
        sa.ForeignKeyConstraint(
            ["repository_id", "failure_run_id", "failure_event_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
            name="fk_collaborations_failure_event",
        )
    )
    groups.append_constraint(
        sa.CheckConstraint(
            "(failure_run_id IS NULL)=(failure_event_seq IS NULL)", name="failure_cause"
        )
    )
    sa.Table(
        "collaboration_messages",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("collaboration_id", sa.Uuid, nullable=False),
        sa.Column("sender_run_id", sa.Uuid, nullable=False),
        sa.Column("receiver_run_id", sa.Uuid, nullable=False),
        sa.Column("receiver_task_id", sa.Uuid, nullable=False),
        sa.Column("envelope_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("payload_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("sent_commit_id", sa.String(64), nullable=False),
        sa.Column("sent_seq", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "collaboration_id"],
            ["collaborations.repository_id", "collaborations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "sender_run_id", "sent_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "sent_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "receiver_task_id"],
            ["collaboration_tasks.repository_id", "collaboration_tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "receiver_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.CheckConstraint("sender_run_id<>receiver_run_id AND sent_seq>0", name="route"),
        sa.Index("ix_collaboration_messages_inbox", "repository_id", "receiver_run_id"),
        sa.Index("ix_collaboration_messages_sender", "repository_id", "sender_run_id"),
        sa.Index("ix_collaboration_messages_envelope", "envelope_ref"),
        sa.Index("ix_collaboration_messages_payload", "payload_ref"),
    )
    sa.Table(
        "collaboration_receipts",
        metadata,
        sa.Column("message_id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("receiver_run_id", sa.Uuid, nullable=False),
        sa.Column("received_commit_id", sa.String(64), nullable=False),
        sa.Column("received_seq", sa.BigInteger, nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "message_id"],
            ["collaboration_messages.repository_id", "collaboration_messages.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "receiver_run_id", "received_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "received_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint("received_seq>0", name="sequence"),
    )
    sa.Table(
        "collaboration_publications",
        metadata,
        sa.Column("repository_id", sa.Uuid, primary_key=True),
        sa.Column("collaboration_id", sa.Uuid, primary_key=True),
        sa.Column("path", sa.String(100), primary_key=True),
        sa.Column("version", sa.BigInteger, primary_key=True),
        sa.Column("publisher_run_id", sa.Uuid, nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.Column("event_seq", sa.BigInteger, nullable=False),
        sa.Column("head_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("object_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "collaboration_id"],
            ["collaborations.repository_id", "collaborations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "publisher_run_id", "event_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint(
            "version>0 AND version<=9007199254740991 AND event_seq>0", name="version"
        ),
        sa.CheckConstraint(
            "path IN ('evidence.json','verification.json','report.md','sources.json')", name="path"
        ),
        sa.Index("ix_collaboration_publications_head", "head_ref"),
        sa.Index("ix_collaboration_publications_object", "object_ref"),
    )
    return metadata
