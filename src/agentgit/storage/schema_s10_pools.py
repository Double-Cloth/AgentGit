"""S10 预注册确认/回归条件与一次性开启；不改写已冻结缩减定义。"""

import sqlalchemy as sa

from agentgit.storage.schema_s10_reduction import STUDY_STATUSES
from agentgit.storage.schema_s10_reduction import build_metadata as build_reduction

TABLE_ORDER = (
    "repair_validation_suites",
    "repair_validation_samples",
    "repair_validation_openings",
)
POOL_STUDY_STATUSES = (*STUDY_STATUSES, "validation_ready")


def build_metadata() -> sa.MetaData:
    metadata = build_reduction()
    control = metadata.tables["repair_study_jobs"]
    for constraint in tuple(control.constraints):
        if isinstance(constraint, sa.CheckConstraint) and "status IN" in str(constraint.sqltext):
            control.constraints.remove(constraint)
            control.append_constraint(
                sa.CheckConstraint(
                    "status IN (" + ",".join(f"'{s}'" for s in POOL_STUDY_STATUSES) + ")",
                    name=constraint.name,
                )
            )
    sa.Table(
        "repair_validation_suites",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("search_id", sa.Uuid, nullable=False),
        sa.Column("manifest_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("repository_id", "search_id"),
        sa.UniqueConstraint("actor", "request_key"),
        sa.ForeignKeyConstraint(
            ["repository_id", "search_id"], ["repair_searches.repository_id", "repair_searches.id"]
        ),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 200 AND length(request_key) BETWEEN 1 AND 200",
            name="validation_suite_actor",
        ),
    )
    sa.Table(
        "repair_validation_samples",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("suite_id", sa.Uuid, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column(
            "generation_ref",
            sa.String(64),
            sa.ForeignKey("objects.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "sample_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False, unique=True
        ),
        sa.Column("source_run_id", sa.Uuid, nullable=False),
        sa.Column("base_commit_id", sa.String(64), nullable=False),
        sa.Column("task_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("validator_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.UniqueConstraint("suite_id", "ordinal"),
        sa.ForeignKeyConstraint(
            ["repository_id", "suite_id"],
            ["repair_validation_suites.repository_id", "repair_validation_suites.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "base_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint(
            "(phase='confirm' AND ordinal BETWEEN 0 AND 5) OR "
            "(phase='regression' AND ordinal BETWEEN 6 AND 9)",
            name="validation_sample_phase",
        ),
    )
    sa.Table(
        "repair_validation_openings",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("suite_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("opening_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("plan_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("event_id", sa.Uuid, nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("actor", "request_key"),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 200 AND length(request_key) BETWEEN 1 AND 200",
            name="validation_opening_actor",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_studies.repository_id", "repair_studies.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "suite_id"],
            ["repair_validation_suites.repository_id", "repair_validation_suites.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id", "event_id"],
            [
                "repair_study_events.repository_id",
                "repair_study_events.study_id",
                "repair_study_events.id",
            ],
        ),
    )
    return metadata
