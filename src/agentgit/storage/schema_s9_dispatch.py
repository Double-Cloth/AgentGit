"""S9 派发控制及未决试验标记；不改写已冻结的 0017 元数据。"""

import sqlalchemy as sa

from agentgit.storage.schema_s9 import TERMINAL_SEARCHES
from agentgit.storage.schema_s9 import build_metadata as build_ledger

SEARCH_STATUSES = ("queued", "running", "cancelling", "waiting_reconciliation", *TERMINAL_SEARCHES)


def build_metadata() -> sa.MetaData:
    metadata = build_ledger()
    jobs = metadata.tables["repair_jobs"]
    previous = next(
        constraint
        for constraint in jobs.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and str(constraint.name).endswith("repair_job_status")
    )
    jobs.constraints.remove(previous)
    jobs.append_constraint(
        sa.CheckConstraint(
            "status IN (" + ",".join(f"'{value}'" for value in SEARCH_STATUSES) + ")",
            name="repair_job_status",
        )
    )
    sa.Table(
        "repair_trial_waits",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("payload_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_trials.repository_id", "repair_trials.id"]
        ),
        sa.CheckConstraint("length(reason) BETWEEN 1 AND 200", name="repair_wait_reason"),
    )
    return metadata
