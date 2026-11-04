"""Recorded 协作来源与新身份的不可变关联。"""

import sqlalchemy as sa

from agentgit.storage.schema_collaboration_exchange import build_metadata as build_previous

TABLE_ORDER = ("collaboration_recordings",)


def build_metadata() -> sa.MetaData:
    metadata = build_previous()
    sa.Table(
        "collaboration_recordings",
        metadata,
        sa.Column("repository_id", sa.Uuid, primary_key=True),
        sa.Column("collaboration_id", sa.Uuid, primary_key=True),
        sa.Column("source_collaboration_id", sa.Uuid, nullable=False),
        sa.Column("source_index_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("identity_map_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "collaboration_id"],
            ["collaborations.repository_id", "collaborations.id"],
            name="fk_collaboration_recordings_target",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_collaboration_id"],
            ["collaborations.repository_id", "collaborations.id"],
            name="fk_collaboration_recordings_source",
        ),
        sa.CheckConstraint("collaboration_id<>source_collaboration_id", name="independent"),
        sa.Index("ix_collaboration_recordings_source", "repository_id", "source_collaboration_id"),
        sa.Index("ix_collaboration_recordings_index", "source_index_ref"),
        sa.Index("ix_collaboration_recordings_map", "identity_map_ref"),
    )
    return metadata
