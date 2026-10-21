"""S4 容器执行意图与操作结果；既有迁移快照保持冻结。"""

import sqlalchemy as sa

from agentgit.storage.schema_s4_resolutions import build_metadata as build_resolutions


def build_metadata() -> sa.MetaData:
    metadata = build_resolutions()
    sa.Table(
        "sandbox_executions",
        metadata,
        sa.Column("call_id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("checkpoint_commit_id", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("policy_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("input_manifest_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("image_id", sa.String(71), nullable=False),
        sa.Column("container_name", sa.String(200), nullable=False, unique=True),
        sa.Column("container_id", sa.String(64)),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("fencing_token", sa.BigInteger, nullable=False),
        sa.Column("effective_timeout_ms", sa.Integer, nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="intent"),
        sa.Column("cleanup_confirmed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("response_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("result_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("metadata_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("repository_id", "call_id"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "call_id"], ["external_calls.repository_id", "external_calls.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "checkpoint_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint("attempt >= 0 AND fencing_token > 0", name="execution_identity"),
        sa.CheckConstraint("effective_timeout_ms BETWEEN 1 AND 120000", name="execution_timeout"),
        sa.CheckConstraint(
            "status IN ('intent','running','complete','outcome_unknown')", name="execution_status"
        ),
        sa.CheckConstraint(
            "status <> 'complete' OR (cleanup_confirmed AND response_ref IS NOT NULL "
            "AND result_ref IS NOT NULL AND metadata_ref IS NOT NULL AND finished_at IS NOT NULL)",
            name="execution_complete",
        ),
        sa.Index("ix_sandbox_execution_run", "repository_id", "run_id", "status"),
        sa.Index("ix_sandbox_execution_job", "repository_id", "job_id"),
        sa.Index("ix_sandbox_execution_checkpoint", "repository_id", "checkpoint_commit_id"),
        *[
            sa.Index(f"ix_sandbox_execution_{name}", name)
            for name in (
                "request_digest",
                "policy_ref",
                "input_manifest_ref",
                "response_ref",
                "result_ref",
                "metadata_ref",
            )
        ],
    )
    return metadata
