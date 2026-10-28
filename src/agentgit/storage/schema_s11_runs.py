"""S11 合并分支与新执行输入的不可变来源，不借用历史 Run。"""

from typing import Any

import sqlalchemy as sa

from agentgit.storage.schema_s11_approval import build_metadata as build_approval

TABLE_ORDER = ("merge_branch_origins", "merge_run_requests")


def build_metadata() -> sa.MetaData:
    metadata = build_approval()

    def common() -> list[Any]:
        return [
            sa.Column("id", sa.Uuid, primary_key=True),
            sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
            sa.Column("branch_name", sa.String(100), nullable=False),
            sa.Column("approval_id", sa.Uuid, nullable=False),
            sa.Column("commit_id", sa.String(64), nullable=False),
            sa.Column("request_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
            sa.Column("actor", sa.String(200), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["repository_id", "approval_id"],
                ["merge_approvals.repository_id", "merge_approvals.id"],
            ),
            sa.ForeignKeyConstraint(
                ["repository_id", "commit_id"], ["commits.repository_id", "commits.id"]
            ),
            sa.UniqueConstraint("repository_id", "id"),
        ]

    sa.Table(
        "merge_branch_origins",
        metadata,
        *common(),
        sa.ForeignKeyConstraint(
            ["repository_id", "branch_name"], ["branches.repository_id", "branches.name"]
        ),
        sa.UniqueConstraint("repository_id", "branch_name"),
    )
    sa.Table(
        "merge_run_requests",
        metadata,
        *common(),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("config_id", sa.Uuid, nullable=False),
        sa.Column("config_version", sa.BigInteger, nullable=False),
        sa.Column("validator_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("initial_state_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "branch_name"],
            ["merge_branch_origins.repository_id", "merge_branch_origins.branch_name"],
        ),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "config_id"], ["agent_configs.repository_id", "agent_configs.id"]
        ),
        sa.CheckConstraint("id=job_id AND config_version>=1", name="merge_run_identity"),
        sa.UniqueConstraint("repository_id", "branch_name"),
    )
    return metadata
