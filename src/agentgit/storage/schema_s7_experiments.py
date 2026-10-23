"""S7 冻结实验和独立 trial 归属；运行状态继续以 Job/Run 为准。"""

import sqlalchemy as sa

from agentgit.storage.schema_s5_sources import build_metadata as build_sources


def build_metadata() -> sa.MetaData:
    metadata = build_sources()
    sa.Table(
        "experiments",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("source_run_id", sa.Uuid, nullable=False),
        sa.Column("from_commit_id", sa.String(64), nullable=False),
        sa.Column("manifest_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("validator_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("repeats", sa.Integer, nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "from_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint("repeats BETWEEN 1 AND 10", name="experiment_repeats"),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 200", name="experiment_actor"),
        sa.Index("ix_experiment_source", "repository_id", "source_run_id"),
        sa.Index("ix_experiment_commit", "repository_id", "from_commit_id"),
        sa.Index("ix_experiment_manifest", "manifest_ref"),
        sa.Index("ix_experiment_validator", "validator_ref"),
    )
    sa.Table(
        "experiment_trials",
        metadata,
        sa.Column("experiment_id", sa.Uuid, primary_key=True),
        sa.Column("group_key", sa.String(1), primary_key=True),
        sa.Column("trial_index", sa.Integer, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("initial_run_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("branch_name", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "experiment_id"], ["experiments.repository_id", "experiments.id"]
        ),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "initial_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "branch_name"], ["branches.repository_id", "branches.name"]
        ),
        sa.CheckConstraint("group_key IN ('A','B')", name="experiment_group"),
        sa.CheckConstraint("trial_index BETWEEN 0 AND 9", name="experiment_trial_index"),
        sa.Index("ix_experiment_trials_repository", "repository_id", "experiment_id"),
        sa.Index("ix_experiment_trials_job", "repository_id", "job_id"),
        sa.Index("ix_experiment_trials_run", "repository_id", "initial_run_id"),
        sa.Index("ix_experiment_trials_branch", "repository_id", "branch_name"),
    )
    return metadata
