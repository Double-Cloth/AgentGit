"""Recorded 来源检查点与实际新检查点的原子关联。"""

import sqlalchemy as sa

from agentgit.storage.schema_collaboration_recordings import build_metadata as build_previous

TABLE_ORDER = ("collaboration_recorded_steps",)


def build_metadata() -> sa.MetaData:
    metadata = build_previous()
    sa.Table(
        "collaboration_recorded_steps",
        metadata,
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("run_id", sa.Uuid, primary_key=True),
        sa.Column("source_commit_id", sa.String(64), primary_key=True),
        sa.Column("source_run_id", sa.Uuid, nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.Column("source_index_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "commit_id"],
            ["snapshots.run_id", "snapshots.commit_id"],
            name="fk_recorded_steps_target",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id", "source_commit_id"],
            ["snapshots.run_id", "snapshots.commit_id"],
            name="fk_recorded_steps_source",
        ),
        sa.UniqueConstraint("run_id", "commit_id"),
        sa.Index("ix_recorded_steps_index", "source_index_ref"),
    )
    return metadata
