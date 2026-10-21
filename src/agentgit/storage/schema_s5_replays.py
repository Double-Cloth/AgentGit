"""S5 耐久重放来源和逐检查点证明；实际计量不进入逻辑状态。"""

import sqlalchemy as sa

from agentgit.storage.schema_s5_versions import build_metadata as build_versions


def build_metadata() -> sa.MetaData:
    metadata = build_versions()
    sa.Table(
        "replay_requests",
        metadata,
        sa.Column("job_id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("source_run_id", sa.Uuid, nullable=False),
        sa.Column("from_commit_id", sa.String(64), nullable=False),
        sa.Column("until_commit_id", sa.String(64)),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("environment_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "job_id"),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "from_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_replay_request_from_commit",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "until_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_replay_request_until_commit",
        ),
        sa.CheckConstraint("mode IN ('recorded','live')", name="replay_mode"),
        sa.CheckConstraint(
            "mode <> 'recorded' OR until_commit_id IS NOT NULL", name="recorded_until"
        ),
        sa.Index("ix_replay_request_source", "repository_id", "source_run_id"),
        sa.Index("ix_replay_request_from", "repository_id", "from_commit_id"),
        sa.Index("ix_replay_request_until", "repository_id", "until_commit_id"),
        sa.Index("ix_replay_request_environment", "environment_ref"),
    )
    sa.Table(
        "replay_checkpoints",
        metadata,
        sa.Column("job_id", sa.Uuid, primary_key=True),
        sa.Column("source_order", sa.BigInteger, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("source_commit_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.Column("state_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("elapsed_ms", sa.BigInteger, nullable=False),
        sa.Column("actual_model_calls", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("actual_tool_calls", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("actual_cost_micro_units", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "job_id"], ["replay_requests.repository_id", "replay_requests.job_id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_replay_checkpoint_source_commit",
        ),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_replay_checkpoint_target_commit",
        ),
        sa.CheckConstraint("source_order BETWEEN 0 AND 10000", name="replay_source_order"),
        sa.CheckConstraint("elapsed_ms BETWEEN 0 AND 9007199254740991", name="replay_elapsed"),
        sa.CheckConstraint(
            "actual_model_calls=0 AND actual_tool_calls=0 AND actual_cost_micro_units=0",
            name="recorded_actual_cost",
        ),
        sa.Index("ix_replay_checkpoint_source", "repository_id", "source_commit_id"),
        sa.Index("ix_replay_checkpoint_run", "repository_id", "run_id"),
        sa.Index("ix_replay_checkpoint_commit", "repository_id", "commit_id"),
        sa.Index("ix_replay_checkpoint_state", "state_ref"),
    )
    return metadata
