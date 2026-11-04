"""S7 干预目标与内容归属；历史阶段元数据保持冻结。"""

import sqlalchemy as sa

from agentgit.storage.schema_s7_experiments import build_metadata as build_experiments


def build_metadata() -> sa.MetaData:
    metadata = build_experiments()
    sa.Table(
        "interventions",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("source_run_id", sa.Uuid, nullable=False),
        sa.Column("source_call_id", sa.Uuid, nullable=False),
        sa.Column("base_commit_id", sa.String(64), nullable=False),
        sa.Column("target_commit_id", sa.String(64), nullable=False),
        sa.Column("target_event_id", sa.BigInteger, sa.ForeignKey("events.id"), nullable=False),
        sa.Column("target_event_seq", sa.BigInteger, nullable=False),
        sa.Column("operator", sa.String(30), nullable=False),
        sa.Column("effect_policy", sa.String(30), nullable=False),
        *[
            sa.Column(name, sa.String(64), sa.ForeignKey("objects.id"), nullable=False)
            for name in ("definition_ref", "payload_ref", "precondition_ref", "provenance_ref")
        ],
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_call_id"],
            ["external_calls.repository_id", "external_calls.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "base_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_intervention_base_commit",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "target_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_intervention_target_commit",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_run_id", "target_event_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
        ),
        sa.CheckConstraint(
            "operator IN ('PROMPT_PATCH','PLAN_PATCH','TOOL_RESPONSE_PATCH')",
            name="intervention_operator",
        ),
        sa.CheckConstraint(
            "(operator='TOOL_RESPONSE_PATCH' AND effect_policy='test_only') OR "
            "(operator<>'TOOL_RESPONSE_PATCH' AND effect_policy='sandbox_or_approval')",
            name="intervention_effect_policy",
        ),
        *[
            sa.Index(f"ix_intervention_{name}", name)
            for name in (
                "source_call_id",
                "base_commit_id",
                "target_commit_id",
                "target_event_id",
                "definition_ref",
                "payload_ref",
                "precondition_ref",
                "provenance_ref",
            )
        ],
        sa.Index("ix_intervention_source", "repository_id", "source_run_id"),
    )
    return metadata
