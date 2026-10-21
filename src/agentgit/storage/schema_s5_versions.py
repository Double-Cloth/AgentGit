"""S5 Fork 来源作为不可变操作元数据保存；历史 Schema 保持冻结。"""

import sqlalchemy as sa

from agentgit.storage.schema_s4_executions import build_metadata as build_executions


def build_metadata() -> sa.MetaData:
    metadata = build_executions()
    sa.Table(
        "branch_forks",
        metadata,
        sa.Column("repository_id", sa.Uuid, primary_key=True),
        sa.Column("branch_name", sa.String(200), primary_key=True),
        sa.Column("source_run_id", sa.Uuid, nullable=False),
        sa.Column("from_commit_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "branch_name"], ["branches.repository_id", "branches.name"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "from_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.Index("ix_branch_forks_source_run", "repository_id", "source_run_id"),
        sa.Index("ix_branch_forks_commit", "repository_id", "from_commit_id"),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 200", name="fork_actor"),
    )
    return metadata
