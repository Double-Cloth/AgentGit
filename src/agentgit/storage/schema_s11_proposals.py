"""S11 不可变提案、修订、验证、冲突和审阅记录；不改变历史写入规则。"""

from typing import Any

import sqlalchemy as sa

from agentgit.storage.schema_s10_reviews import build_metadata as build_reviews

TABLE_ORDER = (
    "merge_proposals",
    "merge_revisions",
    "merge_validations",
    "merge_conflicts",
    "merge_review_sessions",
    "merge_rejections",
)


def build_metadata() -> sa.MetaData:
    metadata = build_reviews()

    def common(body: str, *, identity: bool = True) -> list[Any]:
        values: list[Any] = [
            sa.Column("id", sa.Uuid, primary_key=True),
            sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
            sa.Column(body, sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("repository_id", "id"),
        ]
        if identity:
            values += [
                sa.Column("actor", sa.String(200), nullable=False),
                sa.Column("route", sa.String(300), nullable=False),
                sa.Column("request_key", sa.String(200), nullable=False),
                sa.Column("request_digest", sa.String(64), nullable=False),
                sa.UniqueConstraint("actor", "route", "request_key"),
                sa.CheckConstraint(
                    "length(actor) BETWEEN 1 AND 200 AND length(request_key) BETWEEN 1 AND 200",
                    name=body + "_identity",
                ),
            ]
        return values

    def proposal() -> list[Any]:
        return [
            sa.Column("proposal_id", sa.Uuid, nullable=False),
            sa.ForeignKeyConstraint(
                ["repository_id", "proposal_id"],
                ["merge_proposals.repository_id", "merge_proposals.id"],
            ),
        ]

    def revision() -> list[Any]:
        return [
            sa.Column("revision_id", sa.Uuid, nullable=False),
            sa.ForeignKeyConstraint(
                ["repository_id", "proposal_id", "revision_id"],
                [
                    "merge_revisions.repository_id",
                    "merge_revisions.proposal_id",
                    "merge_revisions.id",
                ],
            ),
        ]

    pins: list[Any] = []
    for side in ("ours", "theirs"):
        pins += [
            sa.Column(side + "_branch", sa.String(100), nullable=False),
            sa.Column(side + "_run_id", sa.Uuid, nullable=False),
            sa.Column(side + "_head", sa.String(64), nullable=False),
            sa.Column(side + "_version", sa.BigInteger, nullable=False),
            sa.ForeignKeyConstraint(
                ["repository_id", side + "_branch"],
                ["branches.repository_id", "branches.name"],
                name="fk_merge_proposals_" + side + "_branch",
            ),
            sa.ForeignKeyConstraint(
                ["repository_id", side + "_run_id"],
                ["runs.repository_id", "runs.id"],
                name="fk_merge_proposals_" + side + "_run",
            ),
            sa.ForeignKeyConstraint(
                ["repository_id", side + "_head"],
                ["commits.repository_id", "commits.id"],
                name="fk_merge_proposals_" + side + "_head",
            ),
        ]
    sa.Table(
        "merge_proposals",
        metadata,
        *common("manifest_ref"),
        *pins,
        sa.Column("base_commit_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "base_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_merge_proposals_base",
        ),
        sa.Column("validator_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.CheckConstraint(
            "ours_branch<>theirs_branch AND ours_head<>theirs_head "
            "AND ours_version>=0 AND theirs_version>=0",
            name="merge_distinct_heads",
        ),
    )
    sa.Table(
        "merge_revisions",
        metadata,
        *common("revision_ref"),
        *proposal(),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("previous_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("repository_id", "proposal_id", "id"),
        sa.UniqueConstraint("proposal_id", "number"),
        sa.CheckConstraint(
            "number BETWEEN 1 AND 100 AND ((number=1 AND previous_ref IS NULL) "
            "OR (number>1 AND previous_ref IS NOT NULL))",
            name="merge_revision_order",
        ),
    )
    sa.Table(
        "merge_validations",
        metadata,
        *common("validation_ref"),
        *proposal(),
        *revision(),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.UniqueConstraint("repository_id", "proposal_id", "revision_id", "id"),
    )
    sa.Table(
        "merge_conflicts",
        metadata,
        *common("conflict_ref", identity=False),
        *proposal(),
        *revision(),
        sa.Column("validation_id", sa.Uuid, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "proposal_id", "revision_id", "validation_id"],
            [
                "merge_validations.repository_id",
                "merge_validations.proposal_id",
                "merge_validations.revision_id",
                "merge_validations.id",
            ],
        ),
        sa.UniqueConstraint("validation_id", "ordinal"),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 999", name="merge_conflict_ordinal"),
    )
    sa.Table(
        "merge_review_sessions",
        metadata,
        *common("ticket_ref"),
        *proposal(),
        *revision(),
        sa.Column("validation_id", sa.Uuid, nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "proposal_id", "revision_id", "validation_id"],
            [
                "merge_validations.repository_id",
                "merge_validations.proposal_id",
                "merge_validations.revision_id",
                "merge_validations.id",
            ],
        ),
    )
    sa.Table(
        "merge_rejections",
        metadata,
        *common("decision_ref"),
        *proposal(),
        *revision(),
        sa.UniqueConstraint("proposal_id"),
    )
    return metadata
