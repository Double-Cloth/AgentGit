"""S4 调度基线快照；后续字段通过新迁移扩展。"""

import sqlalchemy as sa

from agentgit.storage.schema_s3 import build_metadata as build_s3


def build_metadata() -> sa.MetaData:
    metadata = build_s3()
    runs, branches = metadata.tables["runs"], metadata.tables["branches"]
    runs.append_column(
        sa.Column("state_version", sa.BigInteger, nullable=False, server_default="0")
    )
    runs.append_column(sa.Column("control_request", sa.String(20)))
    runs.append_column(sa.Column("control_status", sa.String(20)))
    runs.append_column(sa.Column("failure_code", sa.String(100)))
    branches.append_column(sa.Column("active_writer_run_id", sa.Uuid))
    sa.Table(
        "jobs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("origin_run_id", sa.Uuid),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("payload_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("worker", sa.String(200)),
        sa.Column("fencing_token", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("heartbeat", sa.DateTime(timezone=True)),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("checkpoint_commit_id", sa.String(64)),
        sa.Column("last_error", sa.String(100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "run_id"], ["runs.repository_id", "runs.id"], name="fk_jobs_run"
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "origin_run_id"],
            ["runs.repository_id", "runs.id"],
            name="fk_jobs_origin",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "checkpoint_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint("attempt >= 0 AND fencing_token >= 0", name="attempt"),
        sa.CheckConstraint("kind = 'research_run'", name="kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','paused','waiting_approval','completed',"
            "'failed','cancelled','outcome_unknown')",
            name="status",
        ),
        sa.CheckConstraint(
            "status <> 'running' OR (worker IS NOT NULL AND lease_until IS NOT NULL "
            "AND fencing_token > 0)",
            name="lease",
        ),
        sa.Index("ix_jobs_claim", "status", "available_at", "lease_until", "created_at"),
        sa.Index("ix_jobs_run", "repository_id", "run_id"),
        sa.Index("ix_jobs_origin", "repository_id", "origin_run_id"),
        sa.Index("ix_jobs_checkpoint", "repository_id", "checkpoint_commit_id"),
        sa.Index("ix_jobs_payload_ref", "payload_ref"),
    )
    sa.Table(
        "idempotency_keys",
        metadata,
        sa.Column("actor", sa.String(200), primary_key=True),
        sa.Column("route", sa.String(300), primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("job_id", sa.Uuid),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.Index("ix_idempotency_repository", "repository_id"),
        sa.Index("ix_idempotency_job", "repository_id", "job_id"),
        sa.Index("ix_idempotency_result_ref", "result_ref"),
    )
    return metadata
