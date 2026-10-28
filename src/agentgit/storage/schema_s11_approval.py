"""S11 原子批准与双父历史元数据；历史迁移快照保持原样。"""

import sqlalchemy as sa

from agentgit.storage.schema_s11_proposals import build_metadata as build_proposals


def build_metadata() -> sa.MetaData:
    metadata = build_proposals()
    parents = metadata.tables["commit_parents"]
    for constraint in tuple(parents.constraints):
        if (
            isinstance(constraint, sa.CheckConstraint)
            and str(constraint.sqltext) == "parent_order = 0"
        ):
            parents.constraints.remove(constraint)
    parents.append_constraint(
        sa.CheckConstraint("parent_order BETWEEN 0 AND 1", name="two_parents")
    )
    parents.append_constraint(
        sa.UniqueConstraint("commit_id", "parent_id", name="uq_distinct_parents")
    )
    sa.Table(
        "merge_approvals",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("proposal_id", sa.Uuid, nullable=False),
        sa.Column("revision_id", sa.Uuid, nullable=False),
        sa.Column("review_id", sa.Uuid, nullable=False),
        sa.Column("decision_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("route", sa.String(300), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("proposal_id"),
        sa.UniqueConstraint("commit_id"),
        sa.UniqueConstraint("actor", "route", "request_key"),
        sa.ForeignKeyConstraint(
            ["repository_id", "proposal_id", "revision_id"],
            ["merge_revisions.repository_id", "merge_revisions.proposal_id", "merge_revisions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "review_id"],
            ["merge_review_sessions.repository_id", "merge_review_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id"],
            ["commits.repository_id", "commits.id"],
        ),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 200 AND length(request_key) BETWEEN 1 AND 200",
            name="merge_approval_identity",
        ),
    )
    return metadata
