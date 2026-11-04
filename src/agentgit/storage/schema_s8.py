"""不可变静态 Debug Report 索引；执行历史继续沿用冻结的 S7 Schema。"""

import sqlalchemy as sa

from agentgit.storage.schema_s7_hybrid import build_metadata as build_hybrid


def build_metadata() -> sa.MetaData:
    metadata = build_hybrid()
    sa.Table(
        "debug_reports",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("failed_run_id", sa.Uuid, nullable=False),
        sa.Column("reference_run_id", sa.Uuid),
        sa.Column("report_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "failed_run_id"],
            ["runs.repository_id", "runs.id"],
            name="fk_debug_report_failed_run",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "reference_run_id"],
            ["runs.repository_id", "runs.id"],
            name="fk_debug_report_reference_run",
        ),
        sa.CheckConstraint("length(actor) > 0", name="debug_report_actor"),
        sa.UniqueConstraint("repository_id", "id"),
        sa.Index("ix_debug_report_project", "repository_id", "id"),
        sa.Index("ix_debug_report_failed_run", "repository_id", "failed_run_id"),
        sa.Index("ix_debug_report_ref", "report_ref"),
    )
    return metadata
