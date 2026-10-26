"""S10 实际独立确认/回归台账与不可变统计摘要。"""

import sqlalchemy as sa

from agentgit.storage.schema_s10_pools import POOL_STUDY_STATUSES
from agentgit.storage.schema_s10_pools import build_metadata as build_pools
from agentgit.storage.schema_s10_reduction import STUDY_TERMINAL as REDUCTION_TERMINAL

TABLE_ORDER = (
    "repair_validation_trials",
    "repair_validation_jobs",
    "repair_validation_results",
    "repair_validation_waits",
    "repair_validation_summaries",
)
STUDY_TERMINAL = (
    *REDUCTION_TERMINAL,
    "rejected",
    "verified_under_fixture",
    "empirically_confirmed",
)
STUDY_STATUSES = (
    *POOL_STUDY_STATUSES,
    "confirming",
    "regressing",
    "rejected",
    "verified_under_fixture",
    "empirically_confirmed",
)


def build_metadata() -> sa.MetaData:
    metadata = build_pools()
    control = metadata.tables["repair_study_jobs"]
    for constraint in tuple(control.constraints):
        if isinstance(constraint, sa.CheckConstraint) and "status IN" in str(constraint.sqltext):
            control.constraints.remove(constraint)
            control.append_constraint(
                sa.CheckConstraint(
                    "status IN (" + ",".join(f"'{s}'" for s in STUDY_STATUSES) + ")",
                    name=constraint.name,
                )
            )
    sa.Table(
        "repair_validation_trials",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("study_id", sa.Uuid, nullable=False),
        sa.Column(
            "sample_id",
            sa.Uuid,
            sa.ForeignKey("repair_validation_samples.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("protocol_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("projection_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("intent_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("intent_key", sa.String(64), nullable=False, unique=True),
        sa.Column("branch_name", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id"], ["repair_studies.repository_id", "repair_studies.id"]
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("study_id", "ordinal"),
        sa.CheckConstraint(
            "(phase='confirm' AND ordinal BETWEEN 0 AND 5) OR "
            "(phase='regression' AND ordinal BETWEEN 6 AND 9)",
            name="validation_trial_phase",
        ),
    )
    sa.Table(
        "repair_validation_jobs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("initial_run_id", sa.Uuid, nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"],
            ["repair_validation_trials.repository_id", "repair_validation_trials.id"],
        ),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "initial_run_id"], ["runs.repository_id", "runs.id"]
        ),
    )
    sa.Table(
        "repair_validation_results",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("result_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("evidence_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("not_started", sa.Boolean, nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        *(
            sa.Column(name, sa.BigInteger, nullable=False)
            for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"],
            ["repair_validation_trials.repository_id", "repair_validation_trials.id"],
        ),
        sa.CheckConstraint(
            "verdict IN ('passed','failed','incomplete')", name="validation_verdict"
        ),
        sa.CheckConstraint(
            "model_calls>=0 AND tool_calls>=0 AND tokens>=0 AND cost_micro_usd>=0",
            name="validation_usage",
        ),
        sa.CheckConstraint(
            "(not_started AND evidence_ref IS NULL AND verdict='incomplete' AND "
            "model_calls=0 AND tool_calls=0 AND tokens=0 AND cost_micro_usd=0) OR "
            "(NOT not_started AND evidence_ref IS NOT NULL)",
            name="validation_evidence",
        ),
    )
    sa.Table(
        "repair_validation_waits",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("payload_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"],
            ["repair_validation_trials.repository_id", "repair_validation_trials.id"],
        ),
    )
    sa.Table(
        "repair_validation_summaries",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("summary_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("event_id", sa.Uuid, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_studies.repository_id", "repair_studies.id"]
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
