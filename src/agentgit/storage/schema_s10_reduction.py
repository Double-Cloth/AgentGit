"""S10 独立编排与缩减台账；S9 搜索和已封存决定保持不可变。"""

from typing import Any

import sqlalchemy as sa

from agentgit.storage.schema_s9_dispatch import build_metadata as build_screening

TABLE_ORDER = (
    "repair_studies",
    "repair_study_jobs",
    "repair_reduction_trials",
    "repair_reduction_jobs",
    "repair_reduction_results",
    "repair_study_events",
    "repair_study_locks",
    "repair_reduction_waits",
)
STUDY_TERMINAL = ("cancelled", "budget_exhausted", "needs_attention", "needs_review")
STUDY_STATUSES = (
    "queued",
    "reducing",
    "cancelling",
    "waiting_reconciliation",
    "awaiting_samples",
    *STUDY_TERMINAL,
)


def build_metadata() -> sa.MetaData:
    metadata = build_screening()

    def created() -> sa.Column[Any]:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    def project() -> sa.Column[Any]:
        return sa.Column("repository_id", sa.Uuid, nullable=False)

    def body(name: str) -> sa.Column[Any]:
        return sa.Column(name, sa.String(64), sa.ForeignKey("objects.id"), nullable=False)

    def study_fk() -> sa.ForeignKeyConstraint:
        return sa.ForeignKeyConstraint(
            ["repository_id", "study_id"], ["repair_studies.repository_id", "repair_studies.id"]
        )

    sa.Table(
        "repair_studies",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("search_id", sa.Uuid, nullable=False),
        sa.Column(
            "selected_proposal_id", sa.Uuid, sa.ForeignKey("repair_proposals.id"), nullable=False
        ),
        sa.Column("selected_trial_id", sa.Uuid, sa.ForeignKey("repair_trials.id"), nullable=False),
        sa.Column("source_event_id", sa.Uuid, sa.ForeignKey("repair_events.id"), nullable=False),
        body("manifest_ref"),
        body("initial_state_ref"),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "search_id"], ["repair_searches.repository_id", "repair_searches.id"]
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("repository_id", "search_id"),
        sa.UniqueConstraint("actor", "request_key"),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 200 AND length(request_key) BETWEEN 1 AND 200",
            name="study_request_scope",
        ),
    )
    sa.Table(
        "repair_study_jobs",
        metadata,
        sa.Column("study_id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued"),
        sa.Column("revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("lease_token", sa.Uuid),
        sa.Column("lease_worker", sa.String(200)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("stop_reason", sa.String(200)),
        body("state_ref"),
        created(),
        study_fk(),
        sa.CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in STUDY_STATUSES) + ")",
            name="study_job_status",
        ),
        sa.CheckConstraint("revision >= 0", name="study_job_revision"),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_worker IS NULL AND lease_until IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_worker IS NOT NULL AND lease_until IS NOT NULL "
            "AND length(lease_worker) BETWEEN 1 AND 200)",
            name="study_job_lease",
        ),
        sa.Index("ix_study_claim", "status", "lease_until", "study_id"),
    )
    sa.Table(
        "repair_reduction_trials",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("study_id", sa.Uuid, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("protocol_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("candidate_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("intent_key", sa.String(64), nullable=False),
        body("intent_ref"),
        sa.Column("branch_name", sa.String(100), nullable=False),
        created(),
        study_fk(),
        sa.UniqueConstraint("repository_id", "study_id", "id"),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("study_id", "ordinal"),
        sa.UniqueConstraint("study_id", "intent_key"),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 99 AND phase='reduce'", name="reduction_trial_scope"
        ),
    )
    sa.Table(
        "repair_reduction_jobs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("initial_run_id", sa.Uuid, nullable=False, unique=True),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"],
            ["repair_reduction_trials.repository_id", "repair_reduction_trials.id"],
        ),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "initial_run_id"], ["runs.repository_id", "runs.id"]
        ),
    )
    sa.Table(
        "repair_reduction_results",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        body("result_ref"),
        sa.Column("evidence_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("not_started", sa.Boolean, nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        *(
            sa.Column(n, sa.BigInteger, nullable=False)
            for n in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
        ),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"],
            ["repair_reduction_trials.repository_id", "repair_reduction_trials.id"],
        ),
        sa.CheckConstraint("verdict IN ('passed','failed','incomplete')", name="reduction_verdict"),
        sa.CheckConstraint(
            "model_calls>=0 AND tool_calls>=0 AND tokens>=0 AND cost_micro_usd>=0",
            name="reduction_usage",
        ),
        sa.CheckConstraint(
            "(not_started AND evidence_ref IS NULL AND verdict='incomplete' AND "
            "model_calls=0 AND tool_calls=0 AND tokens=0 AND cost_micro_usd=0) OR "
            "(NOT not_started AND evidence_ref IS NOT NULL)",
            name="reduction_evidence",
        ),
    )
    sa.Table(
        "repair_study_events",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("study_id", sa.Uuid, nullable=False),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        body("payload_ref"),
        created(),
        study_fk(),
        sa.UniqueConstraint("study_id", "revision"),
        sa.UniqueConstraint("repository_id", "study_id", "id"),
        sa.CheckConstraint("revision>=0", name="study_event_revision"),
    )
    sa.Table(
        "repair_study_locks",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        body("lock_ref"),
        body("summary_ref"),
        sa.Column("selection_event_id", sa.Uuid, nullable=False),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_studies.repository_id", "repair_studies.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id", "selection_event_id"],
            [
                "repair_study_events.repository_id",
                "repair_study_events.study_id",
                "repair_study_events.id",
            ],
        ),
    )
    sa.Table(
        "repair_reduction_waits",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("reason", sa.String(200), nullable=False),
        body("payload_ref"),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"],
            ["repair_reduction_trials.repository_id", "repair_reduction_trials.id"],
        ),
    )
    return metadata
