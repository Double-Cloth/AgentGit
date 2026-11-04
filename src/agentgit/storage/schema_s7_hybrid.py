"""S7 Hybrid 模式及不可变只读观测账本。"""

import sqlalchemy as sa

from agentgit.storage.schema_s7_interventions import build_metadata as build_interventions


def build_metadata() -> sa.MetaData:
    metadata = build_interventions()
    requests = metadata.tables["replay_requests"]
    old = next(
        item for item in requests.constraints if item.name == "ck_replay_requests_replay_mode"
    )
    requests.constraints.remove(old)
    requests.append_constraint(
        sa.CheckConstraint("mode IN ('recorded','live','hybrid')", name="replay_mode")
    )
    sa.Table(
        "hybrid_observations",
        metadata,
        sa.Column("call_id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("source_call_id", sa.Uuid, nullable=False),
        sa.Column("policy_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("provenance_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "call_id"],
            ["external_calls.repository_id", "external_calls.id"],
            name="fk_hybrid_observation_call",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "source_call_id"],
            ["external_calls.repository_id", "external_calls.id"],
            name="fk_hybrid_observation_source",
        ),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.CheckConstraint(
            "kind IN ('recorded','synthetic','fixture','miss')", name="hybrid_observation_kind"
        ),
        sa.Index("ix_hybrid_observation_job", "repository_id", "job_id"),
        sa.Index("ix_hybrid_observation_run", "repository_id", "run_id"),
        sa.Index("ix_hybrid_observation_source", "repository_id", "source_call_id"),
        sa.Index("ix_hybrid_observation_policy", "policy_ref"),
        sa.Index("ix_hybrid_observation_provenance", "provenance_ref"),
    )
    return metadata
