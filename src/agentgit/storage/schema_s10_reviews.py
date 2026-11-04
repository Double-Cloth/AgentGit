"""S10 精确提案、审阅快照与决定；历史执行快照保持冻结。"""

from typing import Any

import sqlalchemy as sa

from agentgit.storage.schema_s10_fields import build_metadata as build_fields

TABLE_ORDER = (
    "repair_adoption_proposals",
    "repair_review_sessions",
    "repair_saved_experiments",
    "proposal_reviews",
)


def build_metadata() -> sa.MetaData:
    metadata = build_fields()

    def project() -> sa.Column[Any]:
        return sa.Column("repository_id", sa.Uuid, nullable=False)

    def body(name: str) -> sa.Column[Any]:
        return sa.Column(name, sa.String(64), sa.ForeignKey("objects.id"), nullable=False)

    def created() -> sa.Column[Any]:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    def identity() -> tuple[Any, ...]:
        return (
            sa.Column("actor", sa.String(200), nullable=False),
            sa.Column("request_key", sa.String(200), nullable=False),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.UniqueConstraint("actor", "request_key"),
        )

    def proposal_fk() -> sa.ForeignKeyConstraint:
        return sa.ForeignKeyConstraint(
            ["repository_id", "proposal_id"],
            ["repair_adoption_proposals.repository_id", "repair_adoption_proposals.id"],
        )

    sa.Table(
        "repair_adoption_proposals",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("study_id", sa.Uuid, nullable=False, unique=True),
        body("manifest_ref"),
        body("lock_ref"),
        body("verification_ref"),
        sa.Column("source_config_id", sa.Uuid, nullable=False),
        sa.Column("source_config_version", sa.Integer, nullable=False),
        sa.Column("after_config_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("has_observation", sa.Boolean, nullable=False),
        sa.Column("production_eligible", sa.Boolean, nullable=False),
        *identity(),
        created(),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id"], ["repair_studies.repository_id", "repair_studies.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_config_id"],
            ["agent_configs.repository_id", "agent_configs.id"],
        ),
        sa.CheckConstraint(
            "source_config_version>0 AND has_observation=(after_config_ref IS NULL)",
            name="adoption_scope",
        ),
    )
    sa.Table(
        "repair_review_sessions",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("proposal_id", sa.Uuid, nullable=False),
        body("ticket_ref"),
        sa.Column("expected_config_version", sa.Integer, nullable=False),
        *identity(),
        created(),
        proposal_fk(),
        sa.UniqueConstraint("repository_id", "id"),
        sa.CheckConstraint("expected_config_version>0", name="review_version"),
    )
    sa.Table(
        "repair_saved_experiments",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("proposal_id", sa.Uuid, nullable=False, unique=True),
        body("definition_ref"),
        sa.Column("actor", sa.String(200), nullable=False),
        created(),
        proposal_fk(),
        sa.UniqueConstraint("repository_id", "id"),
    )
    sa.Table(
        "proposal_reviews",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("proposal_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("review_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("decision", sa.String(20), nullable=False),
        body("decision_ref"),
        sa.Column("new_config_id", sa.Uuid),
        sa.Column("saved_experiment_id", sa.Uuid),
        *identity(),
        created(),
        proposal_fk(),
        sa.ForeignKeyConstraint(
            ["repository_id", "review_id"],
            ["repair_review_sessions.repository_id", "repair_review_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "new_config_id"], ["agent_configs.repository_id", "agent_configs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "saved_experiment_id"],
            ["repair_saved_experiments.repository_id", "repair_saved_experiments.id"],
        ),
        sa.CheckConstraint(
            "decision IN ('approve','reject','save_experiment') AND "
            "((decision='approve' AND new_config_id IS NOT NULL "
            "AND saved_experiment_id IS NULL) OR "
            "(decision='reject' AND new_config_id IS NULL AND saved_experiment_id IS NULL) OR "
            "(decision='save_experiment' AND new_config_id IS NULL "
            "AND saved_experiment_id IS NOT NULL))",
            name="review_effect",
        ),
    )
    return metadata
