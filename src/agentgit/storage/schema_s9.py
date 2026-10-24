"""S9 搜索、租约、试验意图与审计；既有执行表继续使用冻结快照。"""

from typing import Any

import sqlalchemy as sa

from agentgit.storage.schema_s8 import build_metadata as build_diagnosis

TABLE_ORDER = (
    "repair_searches",
    "repair_jobs",
    "repair_proposals",
    "repair_trials",
    "repair_trial_jobs",
    "repair_trial_results",
    "repair_events",
)
TERMINAL_SEARCHES = (
    "needs_confirmation",
    "budget_exhausted",
    "no_solution_in_space",
    "not_found",
    "needs_attention",
    "cancelled",
)


def build_metadata() -> sa.MetaData:
    metadata = build_diagnosis()

    def created() -> sa.Column[Any]:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    def project() -> sa.Column[Any]:
        return sa.Column("repository_id", sa.Uuid, nullable=False)

    def search_fk() -> sa.ForeignKeyConstraint:
        return sa.ForeignKeyConstraint(
            ["repository_id", "search_id"], ["repair_searches.repository_id", "repair_searches.id"]
        )

    def body(name: str) -> sa.Column[Any]:
        return sa.Column(name, sa.String(64), sa.ForeignKey("objects.id"), nullable=False)

    sa.Table(
        "repair_searches",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("failed_run_id", sa.Uuid, nullable=False),
        sa.Column("base_commit_id", sa.String(64), nullable=False),
        sa.Column("source_head_commit_id", sa.String(64), nullable=False),
        body("manifest_ref"),
        body("validator_ref"),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("proposal_count", sa.Integer, nullable=False),
        created(),
        sa.Column("screen_mode", sa.String(10), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "failed_run_id"], ["runs.repository_id", "runs.id"]
        ),
        *[
            sa.ForeignKeyConstraint(
                ["repository_id", column],
                ["commits.repository_id", "commits.id"],
                name=f"fk_repair_search_{column}",
            )
            for column in ("base_commit_id", "source_head_commit_id")
        ],
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("actor", "request_key"),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 200 AND length(request_key) BETWEEN 1 AND 200",
            name="repair_request_scope",
        ),
        sa.CheckConstraint("proposal_count BETWEEN 0 AND 64", name="repair_space_size"),
        sa.CheckConstraint("screen_mode IN ('live','hybrid')", name="repair_screen_mode"),
        sa.Index("ix_repair_source", "repository_id", "failed_run_id"),
    )
    sa.Table(
        "repair_jobs",
        metadata,
        sa.Column("search_id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued"),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("lease_token", sa.Uuid),
        sa.Column("lease_worker", sa.String(200)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("stop_reason", sa.String(200)),
        created(),
        search_fk(),
        sa.CheckConstraint("revision >= 0", name="repair_job_revision"),
        sa.CheckConstraint(
            "status IN ('queued','running','cancelling',"
            + ",".join(f"'{value}'" for value in TERMINAL_SEARCHES)
            + ")",
            name="repair_job_status",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_worker IS NULL AND lease_until IS NULL) "
            "OR (lease_token IS NOT NULL AND lease_worker IS NOT NULL "
            "AND lease_until IS NOT NULL AND length(lease_worker) BETWEEN 1 AND 200)",
            name="repair_job_lease",
        ),
        sa.Index("ix_repair_claim", "status", "lease_until", "search_id"),
    )
    sa.Table(
        "repair_proposals",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("search_id", sa.Uuid, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("proposal_key", sa.String(64), nullable=False),
        body("definition_ref"),
        created(),
        search_fk(),
        sa.UniqueConstraint("repository_id", "search_id", "id"),
        sa.UniqueConstraint("repository_id", "search_id", "proposal_key"),
        sa.UniqueConstraint("search_id", "ordinal"),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 63", name="repair_proposal_ordinal"),
    )
    sa.Table(
        "repair_trials",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("search_id", sa.Uuid, nullable=False),
        sa.Column("proposal_id", sa.Uuid),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("branch_name", sa.String(100), nullable=False),
        sa.Column("intent_key", sa.String(64), nullable=False),
        body("intent_ref"),
        created(),
        search_fk(),
        sa.ForeignKeyConstraint(
            ["repository_id", "search_id", "proposal_id"],
            ["repair_proposals.repository_id", "repair_proposals.search_id", "repair_proposals.id"],
        ),
        sa.UniqueConstraint("repository_id", "search_id", "id"),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("search_id", "intent_key"),
        sa.UniqueConstraint("search_id", "ordinal"),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 399", name="repair_trial_ordinal"),
        sa.CheckConstraint("phase='screen'", name="repair_trial_phase"),
        sa.CheckConstraint(
            "(operation='recorded_preflight' AND proposal_id IS NULL) OR "
            "(operation='screen' AND proposal_id IS NOT NULL)",
            name="repair_trial_operation",
        ),
    )
    sa.Table(
        "repair_trial_jobs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("initial_run_id", sa.Uuid, nullable=False, unique=True),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_trials.repository_id", "repair_trials.id"]
        ),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "initial_run_id"], ["runs.repository_id", "runs.id"]
        ),
    )
    sa.Table(
        "repair_trial_results",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("verdict", sa.String(30), nullable=False),
        body("result_ref"),
        sa.Column("model_calls", sa.BigInteger, nullable=False),
        sa.Column("tool_calls", sa.BigInteger, nullable=False),
        sa.Column("tokens", sa.BigInteger, nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger, nullable=False),
        created(),
        sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_trials.repository_id", "repair_trials.id"]
        ),
        sa.CheckConstraint(
            "verdict IN ('chain_valid','passed','failed','not_started')",
            name="repair_trial_verdict",
        ),
        sa.CheckConstraint(
            "model_calls >= 0 AND tool_calls >= 0 AND tokens >= 0 AND cost_micro_usd >= 0",
            name="repair_trial_usage",
        ),
    )
    sa.Table(
        "repair_events",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("search_id", sa.Uuid, nullable=False),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        body("payload_ref"),
        created(),
        search_fk(),
        sa.UniqueConstraint("search_id", "revision"),
        sa.CheckConstraint("revision >= 0", name="repair_event_revision"),
    )
    return metadata
