"""S4 审批基线；历史迁移通过此冻结快照建表。"""

import sqlalchemy as sa

from agentgit.storage.schema_s4_budget import build_metadata as build_budget


def build_metadata() -> sa.MetaData:
    metadata = build_budget()
    sa.Table(
        "approvals",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("checkpoint_commit_id", sa.String(64)),
        sa.Column("call_digest", sa.String(64), nullable=False),
        sa.Column("action_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("policy_digest", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("resume_status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decision", sa.String(20)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_call_id", sa.Uuid),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("run_id", "call_digest"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "checkpoint_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "consumed_call_id"],
            ["external_calls.repository_id", "external_calls.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired','consumed')", name="status"
        ),
        sa.CheckConstraint("resume_status IN ('planning','running')", name="resume"),
        sa.CheckConstraint("decision IS NULL OR decision IN ('approve','reject')", name="decision"),
        sa.CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL AND consumed_call_id IS NOT NULL)",
            name="consumption",
        ),
        sa.Index("ix_approvals_run", "repository_id", "run_id", "created_at"),
        sa.Index("ix_approvals_expiry", "status", "expires_at"),
        sa.Index("ix_approvals_checkpoint", "repository_id", "checkpoint_commit_id"),
        sa.Index("ix_approvals_action", "action_ref"),
        sa.Index("ix_approvals_policy", "policy_digest"),
        sa.Index("ix_approvals_call", "repository_id", "consumed_call_id"),
    )
    return metadata
