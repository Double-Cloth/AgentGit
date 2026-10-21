"""S4 可信核对回执基线；原未知记录与新核对证据分别保留。"""

import sqlalchemy as sa

from agentgit.storage.schema_s4_approvals import build_metadata as build_approvals


def build_metadata() -> sa.MetaData:
    metadata = build_approvals()
    sa.Table(
        "call_resolutions",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("call_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("checkpoint_commit_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("resolver_signature", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("result_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("evidence_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "call_id"], ["external_calls.repository_id", "external_calls.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "checkpoint_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.Index("ix_call_resolutions_run", "repository_id", "run_id", "checkpoint_commit_id"),
        sa.Index("ix_call_resolutions_call", "repository_id", "call_id"),
        sa.Index("ix_call_resolutions_checkpoint", "repository_id", "checkpoint_commit_id"),
        sa.Index("ix_call_resolutions_request", "request_digest"),
        sa.Index("ix_call_resolutions_result", "result_ref"),
        sa.Index("ix_call_resolutions_evidence", "evidence_ref"),
    )
    return metadata
